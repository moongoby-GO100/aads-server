"""
AADS-190: 동적 스케줄러 도구.
CEO 채팅에서 예약 작업을 추가/삭제/조회.

동작 방식:
- APScheduler (main.py에서 기동) 인스턴스를 공유
- 작업 유형: cron(반복), interval(주기), once(1회)
- 실행 내용: run_remote_command 기반 원격 명령 또는 URL 헬스체크
- 결과는 Telegram으로 알림
"""
from __future__ import annotations

import logging
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


async def _execute_scheduled_job(job_id: str, action_type: str, action_config: Dict[str, Any]):
    """예약 작업 실행 핸들러."""
    try:
        result = ""

        if action_type == "remote_command":
            from app.api.ceo_chat_tools import tool_run_remote_command
            project = action_config.get("project", "KIS")
            command = action_config.get("command", "")
            result = await tool_run_remote_command(project, command)

        elif action_type == "health_check":
            from app.services.tool_executor import ToolExecutor
            executor = ToolExecutor()
            result = await executor.execute("health_check", {"server": "all"})

        elif action_type == "db_query":
            from app.api.ceo_chat_tools_db import query_project_database
            project = action_config.get("project", "KIS")
            query = action_config.get("query", "")
            r = await query_project_database(project, query, limit=10)
            result = str(r)

        elif action_type == "url_check":
            import httpx
            url = action_config.get("url", "")
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url)
                result = f"URL {url} → {r.status_code} ({len(r.content)} bytes)"

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
    """
    if not _scheduler:
        return {"error": "스케줄러가 초기화되지 않았습니다"}

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

    # 기존 작업 중복 체크
    existing = _scheduler.get_job(job_id)
    if existing:
        return {"error": f"이름 '{name}'의 작업이 이미 존재합니다. 삭제 후 다시 등록하세요."}

    try:
        if schedule_type == "cron":
            from apscheduler.triggers.cron import CronTrigger
            # KST → UTC 변환 (KST = UTC+9)
            hour_kst = schedule_config.get("hour", 9)
            minute = schedule_config.get("minute", 0)
            day_of_week = schedule_config.get("day_of_week", "mon-fri")
            hour_utc = (hour_kst - 9) % 24

            _scheduler.add_job(
                _execute_scheduled_job,
                CronTrigger(
                    hour=hour_utc, minute=minute,
                    day_of_week=day_of_week, timezone="UTC"
                ),
                args=[job_id, action_type, action_config],
                id=job_id,
            )
            desc = f"cron: {day_of_week} {hour_kst:02d}:{minute:02d} KST"

        elif schedule_type == "interval":
            minutes = schedule_config.get("minutes", 0)
            hours = schedule_config.get("hours", 0)
            if not minutes and not hours:
                return {"error": "interval에는 minutes 또는 hours가 필요합니다"}

            _scheduler.add_job(
                _execute_scheduled_job,
                "interval",
                minutes=minutes if minutes else hours * 60,
                args=[job_id, action_type, action_config],
                id=job_id,
            )
            desc = f"interval: {'매 ' + str(minutes) + '분' if minutes else '매 ' + str(hours) + '시간'}"

        elif schedule_type == "once":
            delay = schedule_config.get("delay_minutes", 1)
            run_time = datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(minutes=delay)

            _scheduler.add_job(
                _execute_scheduled_job,
                "date",
                run_date=run_time,
                args=[job_id, action_type, action_config],
                id=job_id,
            )
            desc = f"once: {run_time.strftime('%Y-%m-%d %H:%M KST')}"

        logger.info(f"schedule_task: registered | job={job_id} {desc}")
        return {
            "status": "registered",
            "job_id": job_id,
            "name": name,
            "schedule": desc,
            "action_type": action_type,
            "action_config": action_config,
        }

    except Exception as e:
        return {"error": f"스케줄 등록 실패: {str(e)}"}


async def unschedule_task(name: str) -> Dict[str, Any]:
    """예약 작업 삭제."""
    if not _scheduler:
        return {"error": "스케줄러가 초기화되지 않았습니다"}

    job_id = f"user_{name.strip().replace(' ', '_')}"
    job = _scheduler.get_job(job_id)
    if not job:
        return {"error": f"작업 '{name}' (id={job_id})을 찾을 수 없습니다"}

    _scheduler.remove_job(job_id)
    logger.info(f"unschedule_task: removed | job={job_id}")
    return {"status": "removed", "job_id": job_id, "name": name}


async def list_scheduled_tasks() -> Dict[str, Any]:
    """등록된 예약 작업 목록 조회."""
    if not _scheduler:
        return {"error": "스케줄러가 초기화되지 않았습니다"}

    jobs = _scheduler.get_jobs()
    result = []
    for job in jobs:
        next_run = job.next_run_time
        result.append({
            "job_id": job.id,
            "name": job.id.replace("user_", "") if job.id.startswith("user_") else job.id,
            "trigger": str(job.trigger),
            "next_run": next_run.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST") if next_run else "N/A",
            "is_user_job": job.id.startswith("user_"),
        })

    return {
        "total": len(result),
        "system_jobs": len([j for j in result if not j["is_user_job"]]),
        "user_jobs": len([j for j in result if j["is_user_job"]]),
        "jobs": result,
    }
