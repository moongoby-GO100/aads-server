"""자동화 예약 — 기존 스케줄러(`ceo_chat_tools_scheduler`)의 cron(KST)을 그대로 쓴다 (설계서 7.7).

- 승인 전에는 등록한 직후 일시정지(비활성)로 둔다. 승인되면 재개한다.
- 한 번만(once) 실행은 지금부터의 지연(delay)으로만 등록할 수 있어, 비활성 등록이 의미가 없다.
  활성화 시점에 등록한다.
- KST 가 9시 이전이면 UTC 로는 전날이다. 기존 스케줄러는 시각만 바꾸고 요일은 그대로 두므로
  요일 지정(평일·매주)은 여기서 하루 앞당겨서 넘긴다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services.automation_intent_parser import DAY_NAMES

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
ACTION_TYPE = "automation_run"
JOB_PREFIX = "user_"
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")


class ScheduleUnavailable(Exception):
    """스케줄러에 닿을 수 없거나 등록이 거부됐다."""


def job_name(draft_id: str) -> str:
    return f"automation_{draft_id}"


def job_id_of(draft_id: str) -> str:
    return f"{JOB_PREFIX}{job_name(draft_id)}"


def _shift_back(days: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(DAY_NAMES[(DAY_NAMES.index(day) - 1) % 7] for day in days)


def cron_config(schedule: dict[str, Any]) -> dict[str, Any]:
    """{kind,hour,minute,day_of_week?} → schedule_task_on 의 cron 설정. 요일은 UTC 기준으로 보정."""
    kind = schedule["kind"]
    hour, minute = int(schedule["hour"]), int(schedule["minute"])
    if kind == "daily":
        days: tuple[str, ...] = DAY_NAMES
    elif kind == "weekdays":
        days = _WEEKDAYS
    elif kind == "weekly":
        days = (str(schedule["day_of_week"]),)
    else:
        raise ValueError("schedule_kind_not_cron")
    if hour < 9:
        days = _shift_back(days)
    return {"hour": hour, "minute": minute, "day_of_week": ",".join(days)}


def once_delay_minutes(schedule: dict[str, Any], *, now: datetime | None = None) -> int:
    """오늘(KST) 해당 시각까지 남은 분. 이미 지났으면 내일 같은 시각. 최소 1분."""
    current = (now or datetime.now(KST)).astimezone(KST)
    target = current.replace(hour=int(schedule["hour"]), minute=int(schedule["minute"]), second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return max(1, int((target - current).total_seconds() // 60))


def _scheduler() -> Any:
    from app.api import ceo_chat_tools_scheduler as sched

    scheduler = sched._ensure_scheduler()
    if scheduler is None:
        raise ScheduleUnavailable("scheduler_unavailable")
    return scheduler


async def register(
    *, draft_id: str, tenant_id: str, owner_user: str, directive: str,
    schedule: dict[str, Any], active: bool,
) -> dict[str, Any]:
    """예약 등록. active=False 면 등록 직후 일시정지. 돌려주는 job_id 는 초안의 schedule_job_id 에 저장한다."""
    from app.api import ceo_chat_tools_scheduler as sched

    scheduler = _scheduler()
    if schedule["kind"] == "once":
        if not active:
            return {"job_id": None, "deferred": True}
        schedule_type, config = "once", {"delay_minutes": once_delay_minutes(schedule)}
    else:
        schedule_type, config = "cron", cron_config(schedule)
    action_config = {
        "tenant_id": str(tenant_id), "draft_id": str(draft_id),
        "owner_user": owner_user, "directive": directive,
    }
    result = await sched.schedule_task_on(
        scheduler, job_name(draft_id), schedule_type, ACTION_TYPE, action_config, config,
        report_to_session=False, trigger_session_reaction=False,
    )
    if result.get("error"):
        logger.warning("automation_schedule_register_failed: draft=%s", str(draft_id)[:8])
        raise ScheduleUnavailable("schedule_register_failed")
    job_id = str(result["job_id"])
    if not active:
        scheduler.pause_job(job_id)
    return {"job_id": job_id, "deferred": False, "persisted": bool(result.get("persisted"))}


def _job(job_id: str) -> Any:
    job = _scheduler().get_job(job_id)
    if job is None:
        raise ScheduleUnavailable("schedule_job_missing")
    return job


def pause(job_id: str) -> None:
    _job(job_id)
    _scheduler().pause_job(job_id)


def resume(job_id: str) -> None:
    _job(job_id)
    _scheduler().resume_job(job_id)


def remove(job_id: str) -> None:
    scheduler = _scheduler()
    if scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)


def next_run_at(job_id: str | None) -> str | None:
    """다음 실행 시각(KST ISO). 예약이 없거나 멈춘 상태면 None. 스케줄러가 없어도 예외를 내지 않는다."""
    if not job_id:
        return None
    try:
        job = _scheduler().get_job(job_id)
    except ScheduleUnavailable:
        return None
    when = getattr(job, "next_run_time", None) if job is not None else None
    return when.astimezone(KST).isoformat() if when else None
