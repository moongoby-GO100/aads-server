"""
AADS-190: 동적 스케줄러 도구.
CEO 채팅에서 예약 작업을 추가/삭제/조회.

동작 방식:
- APScheduler (main.py에서 기동) 인스턴스를 공유
- 작업 유형: cron(반복), interval(주기), once(1회)
- 실행 내용: run_remote_command 기반 원격 명령 또는 URL 헬스체크
- 결과는 Telegram 및 연결된 채팅 세션으로 알림
- API 프로세스 밖(MCP 브리지)에서는 스케줄러를 만들지 않고 API 의 내부 엔드포인트
  (app/api/internal_scheduler.py)에 위임한다 — 영속 jobstore 는 API 프로세스에만 있다.
"""
from __future__ import annotations

import copy
import asyncio
import functools
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# main.py에서 설정되는 전역 scheduler 참조
_scheduler = None


def set_scheduler(sched):
    """main.py에서 호출하여 스케줄러 인스턴스 등록."""
    global _scheduler
    _scheduler = sched


def get_scheduler():
    return _scheduler


def _execute_scheduled_job_sync(job_id: str, action_type: str, action_config: Dict[str, Any]):
    asyncio.run(_execute_scheduled_job(job_id, action_type, action_config))


PERSISTENT_JOBSTORE = "persistent"
PERSISTENT_MISFIRE_GRACE_SECONDS = 3600


def _raw_add_job(scheduler):
    """main.py 가 덮어쓴 클로저 래퍼(pickle 불가)를 우회하는 원본 add_job."""
    main_mod = sys.modules.get("app.main")
    state = getattr(getattr(main_mod, "app", None), "state", None)
    raw = getattr(state, "scheduler_add_job_raw", None)
    if raw is not None and getattr(state, "scheduler", None) is scheduler:
        return raw
    cls_add_job = getattr(type(scheduler), "add_job", None)
    if cls_add_job is not None:
        return functools.partial(cls_add_job, scheduler)
    return scheduler.add_job


def _jsonable(value: Any) -> Any:
    """pickle/JSON 안전한 순수 값만 남긴다."""
    return json.loads(json.dumps(value, default=str))


def _persisted_job_ids(scheduler) -> set:
    try:
        return {job.id for job in scheduler.get_jobs(jobstore=PERSISTENT_JOBSTORE)}
    except Exception as exc:
        logger.warning("list_scheduled_tasks_persistent_lookup_failed: %s", type(exc).__name__)
        return set()


def _ensure_scheduler():
    """API 프로세스의 스케줄러를 반환한다. 없으면(브리지 등) None — 로컬로 만들지 않는다.

    브리지가 만든 BackgroundScheduler 는 메모리 전용이라 세션이 끝나면 예약이 사라졌다.
    """
    global _scheduler
    if _scheduler:
        return _scheduler

    main_mod = sys.modules.get("app.main")
    scheduler = getattr(getattr(getattr(main_mod, "app", None), "state", None), "scheduler", None)
    if scheduler:
        _scheduler = scheduler
        return _scheduler
    return None


SCHEDULER_DELEGATE_PREFIX = "/api/v1/internal/scheduler"
_DELEGATE_TIMEOUT_SECONDS = 15.0


def _delegate_failure(reason: str) -> Dict[str, Any]:
    logger.error("scheduler_delegate_failed: %s", reason)
    return {
        "error": f"예약 작업을 API 스케줄러에 위임하지 못했습니다 ({reason}). 등록/변경되지 않았습니다.",
        "persisted": False,
        "persisted_reason": f"scheduler_delegate_failed: {reason}",
        "delegated": False,
    }


async def _delegate_to_api(method: str, op: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """브리지 → API 프로세스 내부 엔드포인트 호출. 실패해도 메모리 스케줄러로 떨어지지 않는다."""
    import httpx

    monitor_key = os.getenv("AADS_MONITOR_KEY", "").strip()
    if not monitor_key:
        return _delegate_failure("AADS_MONITOR_KEY_missing")
    base = os.getenv("AADS_API_BASE", "http://localhost:8080").rstrip("/")
    url = f"{base}{SCHEDULER_DELEGATE_PREFIX}/{op}"
    try:
        async with httpx.AsyncClient(timeout=_DELEGATE_TIMEOUT_SECONDS) as client:
            resp = await client.request(
                method, url, json=payload if method != "GET" else None,
                headers={"x-monitor-key": monitor_key},
            )
    except Exception as exc:
        return _delegate_failure(f"{type(exc).__name__}: {str(exc)[:120]}")
    if resp.status_code != 200:
        return _delegate_failure(f"http_{resp.status_code}: {resp.text[:120]}")
    try:
        data = resp.json()
    except ValueError:
        return _delegate_failure("invalid_json_response")
    if not isinstance(data, dict):
        return _delegate_failure("unexpected_response_shape")
    data["delegated"] = True
    return data


def _bridge_session_id() -> str:
    try:
        from app.services.tool_executor import current_chat_session_id

        ctx = str(current_chat_session_id.get("") or "").strip()
    except Exception:
        ctx = ""
    return ctx or str(os.getenv("AADS_SESSION_ID", "") or "").strip()


def _job_report_session_id(job) -> str:
    args = getattr(job, "args", None) or ()
    if len(args) >= 3 and isinstance(args[2], dict):
        return str(args[2].get("report_session_id") or "").strip()
    return ""


async def _execute_scheduled_job(job_id: str, action_type: str, action_config: Dict[str, Any]):
    """예약 작업 실행 핸들러."""
    report_session_id = ""
    report_enabled = True
    trigger_session_reaction = False
    action_config = dict(action_config or {})
    callback_meta = {
        "job_id": job_id,
        "action_type": action_type,
        "schedule_callback": True,
    }
    try:
        result = ""
        report_session_id = str(
            action_config.pop("report_session_id", "")
            or action_config.pop("session_report_session_id", "")
            or action_config.pop("chat_session_id", "")
        ).strip()
        report_enabled = bool(action_config.pop("report_to_session", True))
        trigger_session_reaction = bool(
            action_config.pop("trigger_session_reaction", False)
            or action_config.pop("react_to_session_report", False)
        )

        if action_type == "remote_command":
            from app.api.ceo_chat_tools import tool_run_remote_command
            project = action_config.get("project", "GO100")
            command = action_config.get("command", "")
            result = await tool_run_remote_command(project, command)

        elif action_type == "health_check":
            from app.services.tool_executor import ToolExecutor
            executor = ToolExecutor()
            result = await executor.execute("health_check", {"server": "all"})

        elif action_type == "db_query":
            from app.api.ceo_chat_tools_db import query_project_database
            project = action_config.get("project", "GO100")
            query = action_config.get("query", "")
            r = await query_project_database(project, query, limit=10)
            result = str(r)

        elif action_type == "url_check":
            import httpx
            url = action_config.get("url", "")
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url)
                result = f"URL {url} → {r.status_code} ({len(r.content)} bytes)"

        if report_enabled and report_session_id:
            try:
                from app.services.session_reporter import post_session_report

                await post_session_report(
                    session_id=report_session_id,
                    title=f"예약 작업 완료: {job_id}",
                    body=str(result)[:4000],
                    status="done",
                    source="schedule_task",
                    project=str(action_config.get("project") or "AADS"),
                    metadata=callback_meta,
                    intent="auto_report",
                    trigger_reaction=trigger_session_reaction,
                )
            except Exception as report_err:
                logger.warning("scheduler_session_report_failed: job=%s err=%s", job_id, report_err)

        # 텔레그램 알림
        try:
            from app.services.telegram_bot import get_telegram_bot
            bot = get_telegram_bot()
            if bot and bot.is_ready:
                msg = f"⏰ *예약 작업 완료*\n\nJob: `{job_id}`\nType: {action_type}\n\n```\n{str(result)[:500]}\n```"
                await bot.send_message(msg)
        except Exception as e:
            logger.debug(f"scheduler_telegram_notify_failed: {e}")

        logger.info(f"scheduled_job_executed: job={job_id} type={action_type}")

    except Exception as e:
        logger.error(f"scheduled_job_failed: job={job_id} error={e}")
        if report_enabled and report_session_id:
            try:
                from app.services.session_reporter import post_session_report

                await post_session_report(
                    session_id=report_session_id,
                    title=f"예약 작업 실패: {job_id}",
                    body=str(e)[:4000],
                    status="error",
                    source="schedule_task",
                    project=str(action_config.get("project") or "AADS"),
                    metadata=callback_meta,
                    intent="auto_report",
                    trigger_reaction=trigger_session_reaction,
                )
            except Exception as report_err:
                logger.warning("scheduler_session_report_error_failed: job=%s err=%s", job_id, report_err)
        # 실패도 알림
        try:
            from app.services.telegram_bot import get_telegram_bot
            bot = get_telegram_bot()
            if bot and bot.is_ready:
                await bot.send_message(f"🔴 *예약 작업 실패*\n\nJob: `{job_id}`\nError: {str(e)[:300]}")
        except Exception:
            pass


async def schedule_task(
    name: str,
    schedule_type: str,
    action_type: str,
    action_config: Dict[str, Any],
    schedule_config: Optional[Dict[str, Any]] = None,
    report_session_id: str = "",
    report_to_session: bool = True,
    trigger_session_reaction: bool = True,
) -> Dict[str, Any]:
    """예약 작업 등록. API 프로세스면 직접, 브리지면 API 에 위임한다."""
    scheduler = _ensure_scheduler()
    if scheduler:
        return await schedule_task_on(
            scheduler, name, schedule_type, action_type, action_config, schedule_config,
            report_session_id, report_to_session, trigger_session_reaction,
        )
    action_config = action_config or {}
    session_id = str(
        report_session_id
        or action_config.get("report_session_id")
        or action_config.get("session_report_session_id")
        or action_config.get("chat_session_id")
        or _bridge_session_id()
    ).strip()
    return await _delegate_to_api("POST", "schedule", {
        "name": name,
        "schedule_type": schedule_type,
        "action_type": action_type,
        "action_config": action_config,
        "schedule_config": schedule_config,
        "report_session_id": session_id,
        "report_to_session": report_to_session,
        "trigger_session_reaction": trigger_session_reaction,
    })


async def schedule_task_on(
    scheduler,
    name: str,
    schedule_type: str,
    action_type: str,
    action_config: Dict[str, Any],
    schedule_config: Optional[Dict[str, Any]] = None,
    report_session_id: str = "",
    report_to_session: bool = True,
    trigger_session_reaction: bool = True,
    replace_same_session: bool = False,
) -> Dict[str, Any]:
    """
    예약 작업 등록.

    Args:
        name: 작업 이름 (고유 ID로 사용)
        schedule_type: cron, interval, once
        action_type: remote_command, health_check, db_query, url_check
        action_config: 실행 설정 (project, command, query, url 등)
        schedule_config: 스케줄 설정
            - cron: {hour, minute, day_of_week} (KST 기준)
            - interval: {minutes} 또는 {hours}
            - once: {delay_minutes} (지금부터 N분 후 1회)
        report_session_id: 실행 결과를 자동 보고할 chat_sessions.id
        report_to_session: False면 세션 자동보고 비활성화
        trigger_session_reaction: 세션 자동보고 후 해당 세션 AI 후속 반응 트리거
        replace_same_session: 같은 report_session_id 가 만든 동명 작업이면 교체(위임 재시도 멱등용)
    """
    if not name or not name.strip():
        return {"error": "name은 필수입니다"}

    # 유효성 검사
    valid_actions = ("remote_command", "health_check", "db_query", "url_check")
    if action_type not in valid_actions:
        return {"error": f"action_type은 {valid_actions} 중 하나여야 합니다"}

    valid_schedules = ("cron", "interval", "once")
    if schedule_type not in valid_schedules:
        return {"error": f"schedule_type은 {valid_schedules} 중 하나여야 합니다"}

    job_id = f"user_{name.strip().replace(' ', '_')}"
    schedule_config = schedule_config or {}
    try:
        effective_action_config = _jsonable(copy.deepcopy(action_config or {}))
    except (TypeError, ValueError) as exc:
        return {"error": f"action_config 는 JSON 직렬화 가능해야 합니다: {exc}"}
    effective_report_session_id = str(
        report_session_id
        or effective_action_config.get("report_session_id")
        or effective_action_config.get("session_report_session_id")
        or effective_action_config.get("chat_session_id")
        or ""
    ).strip()
    if effective_report_session_id:
        effective_action_config["report_session_id"] = effective_report_session_id
    effective_report_to_session = bool(report_to_session and effective_report_session_id)
    effective_trigger_session_reaction = bool(
        effective_report_to_session and trigger_session_reaction
    )
    effective_action_config["report_to_session"] = effective_report_to_session
    effective_action_config["trigger_session_reaction"] = effective_trigger_session_reaction

    # 기존 작업 중복 체크
    existing = scheduler.get_job(job_id)
    replace = False
    if existing:
        if (
            replace_same_session
            and effective_report_session_id
            and _job_report_session_id(existing) == effective_report_session_id
        ):
            replace = True
            if getattr(existing, "_jobstore_alias", PERSISTENT_JOBSTORE) != PERSISTENT_JOBSTORE:
                scheduler.remove_job(job_id)
        else:
            return {"error": f"이름 '{name}'의 작업이 이미 존재합니다. 삭제 후 다시 등록하세요."}

    persisted = False
    persist_error = ""

    def _register(job_func, *trigger_args, **job_kwargs):
        """persistent 에 먼저 등록하고, 실패하면 메모리로 폴백하되 사유를 남긴다."""
        nonlocal persisted, persist_error
        args = [job_id, action_type, effective_action_config]
        try:
            _raw_add_job(scheduler)(
                job_func,
                *trigger_args,
                args=args,
                id=job_id,
                jobstore=PERSISTENT_JOBSTORE,
                replace_existing=replace,
                misfire_grace_time=PERSISTENT_MISFIRE_GRACE_SECONDS,
                **job_kwargs,
            )
            persisted = True
            return
        except Exception as exc:
            persist_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.error(
                "schedule_task_persist_failed: job=%s reason=%s", job_id, persist_error
            )
        scheduler.add_job(job_func, *trigger_args, args=args, id=job_id, **job_kwargs)

    try:
        job_func = _execute_scheduled_job
        if schedule_type == "cron":
            from apscheduler.triggers.cron import CronTrigger
            # KST → UTC 변환 (KST = UTC+9)
            hour_kst = schedule_config.get("hour", 9)
            minute = schedule_config.get("minute", 0)
            day_of_week = schedule_config.get("day_of_week", "mon-fri")
            hour_utc = (hour_kst - 9) % 24

            _register(
                job_func,
                CronTrigger(
                    hour=hour_utc, minute=minute,
                    day_of_week=day_of_week, timezone="UTC"
                ),
            )
            desc = f"cron: {day_of_week} {hour_kst:02d}:{minute:02d} KST"

        elif schedule_type == "interval":
            minutes = schedule_config.get("minutes", 0)
            hours = schedule_config.get("hours", 0)
            if not minutes and not hours:
                return {"error": "interval에는 minutes 또는 hours가 필요합니다"}

            _register(
                job_func,
                "interval",
                minutes=minutes if minutes else hours * 60,
            )
            desc = f"interval: {'매 ' + str(minutes) + '분' if minutes else '매 ' + str(hours) + '시간'}"

        elif schedule_type == "once":
            delay = schedule_config.get("delay_minutes", 1)
            run_time = datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(minutes=delay)

            _register(job_func, "date", run_date=run_time)
            desc = f"once: {run_time.strftime('%Y-%m-%d %H:%M KST')}"

        logger.info(f"schedule_task: registered | job={job_id} {desc} persisted={persisted}")
        result = {
            "status": "registered",
            "job_id": job_id,
            "name": name,
            "schedule": desc,
            "action_type": action_type,
            "action_config": effective_action_config,
            "report_session_id": effective_report_session_id,
            "report_to_session": effective_report_to_session,
            "trigger_session_reaction": effective_trigger_session_reaction,
            "persisted": persisted,
        }
        if not persisted:
            result["persisted_reason"] = persist_error
        return result

    except Exception as e:
        return {"error": f"스케줄 등록 실패: {str(e)}"}


async def unschedule_task(name: str) -> Dict[str, Any]:
    """예약 작업 삭제. API 프로세스면 직접, 브리지면 API 에 위임한다."""
    scheduler = _ensure_scheduler()
    if scheduler:
        return await unschedule_task_on(scheduler, name)
    return await _delegate_to_api("POST", "unschedule", {"name": name})


async def unschedule_task_on(scheduler, name: str) -> Dict[str, Any]:
    job_id = f"user_{name.strip().replace(' ', '_')}"
    job = scheduler.get_job(job_id)
    if not job:
        return {"error": f"작업 '{name}' (id={job_id})을 찾을 수 없습니다"}

    scheduler.remove_job(job_id)
    logger.info(f"unschedule_task: removed | job={job_id}")
    return {"status": "removed", "job_id": job_id, "name": name}


async def list_scheduled_tasks() -> Dict[str, Any]:
    """등록된 예약 작업 목록 조회. API 프로세스면 직접, 브리지면 API 에 위임한다."""
    scheduler = _ensure_scheduler()
    if scheduler:
        return await list_scheduled_tasks_on(scheduler)
    return await _delegate_to_api("GET", "jobs")


async def list_scheduled_tasks_on(scheduler) -> Dict[str, Any]:
    jobs = scheduler.get_jobs()
    persisted_ids = _persisted_job_ids(scheduler)
    result = []
    for job in jobs:
        next_run = job.next_run_time
        result.append({
            "job_id": job.id,
            "name": job.id.replace("user_", "") if job.id.startswith("user_") else job.id,
            "trigger": str(job.trigger),
            "next_run": next_run.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST") if next_run else "N/A",
            "is_user_job": job.id.startswith("user_"),
            "persisted": job.id in persisted_ids,
        })

    return {
        "total": len(result),
        "system_jobs": len([j for j in result if not j["is_user_job"]]),
        "user_jobs": len([j for j in result if j["is_user_job"]]),
        "jobs": result,
    }
