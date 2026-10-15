"""채팅 스트립용 목표 활동 판정 (PRD goal-priority-dormancy-autoapproval-display-prd §6-1·§7 M2b).

- ``activity``: ``working``(🔄) | ``awaiting``(⏳) | None
    working  = 연결된 러너 작업이 running/claimed 이거나, 연결된 세션에 30분 안에 채팅 활동이 있다.
    awaiting = 연결된 작업 중 awaiting_approval 이 1건 이상. (working 이 우선한다)
- ``milestones[].current_runner``: ``{job_id, status}`` — 그 마일스톤에 연결된 진행·승인대기 작업.
- ``unlinked_count``: 이 세션이 최근 48시간에 제출한 goal_id 없는 러너 작업 수.
- ``strip_rank``: 🔄(0) → ⏳(1) → P0(2) → P1·P2(3) → 💤(4). 작을수록 먼저.

휴면(dormant) 판정은 M2 의 `goal_signals.py` 가 한다 — 여기서 다시 계산하지 않고 그 결과를 받는다.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.services.goal_signals import is_p0, priority_rank

logger = logging.getLogger(__name__)

ACTIVITY_WORKING = "working"
ACTIVITY_AWAITING = "awaiting"

RUNNING_STATUSES = ("running", "claimed")
AWAITING_STATUS = "awaiting_approval"
CHAT_ACTIVE_MINUTES = 30
UNLINKED_WINDOW_HOURS = 48
_CHAT_OPEN_STATUSES = ("draft", "active", "blocked")

RANK_WORKING = 0
RANK_AWAITING = 1
RANK_P0 = 2
RANK_OTHER = 3
RANK_DORMANT = 4

_RUNNER_PICK_ORDER = {"running": 0, "claimed": 1, AWAITING_STATUS: 2}


# ── 순수 판정 ────────────────────────────────────────────────────────────────
def decide_activity(
    *, running: bool = False, awaiting: bool = False, chat_active: bool = False,
) -> Optional[str]:
    if running or chat_active:
        return ACTIVITY_WORKING
    if awaiting:
        return ACTIVITY_AWAITING
    return None


def strip_rank(activity: Optional[str], priority: Any, dormant: bool) -> int:
    """스트립 정렬 순번. 활동이 있으면 휴면·우선순위보다 앞선다."""
    if activity == ACTIVITY_WORKING:
        return RANK_WORKING
    if activity == ACTIVITY_AWAITING:
        return RANK_AWAITING
    if dormant:
        return RANK_DORMANT
    return RANK_P0 if is_p0(priority) else RANK_OTHER


def strip_sort_key(item: Mapping[str, Any]) -> tuple[int, int, str]:
    return (
        int(item.get("strip_rank", RANK_OTHER)),
        priority_rank(item.get("priority")),
        str(item.get("title") or ""),
    )


def sort_for_strip(items: Iterable[Mapping[str, Any]]) -> list[Any]:
    return sorted(items, key=strip_sort_key)


def pick_current_runner(jobs: Iterable[Mapping[str, Any]]) -> Optional[dict[str, str]]:
    """running → claimed → awaiting_approval 순, 같으면 최신 작업."""
    best: Optional[Mapping[str, Any]] = None
    best_key: Optional[tuple[int, float]] = None
    for job in jobs:
        status = str(job.get("status") or "")
        if status not in _RUNNER_PICK_ORDER:
            continue
        created = job.get("created_at")
        stamp = created.timestamp() if hasattr(created, "timestamp") else 0.0
        key = (_RUNNER_PICK_ORDER[status], -stamp)
        if best_key is None or key < best_key:
            best, best_key = job, key
    if best is None:
        return None
    return {"job_id": str(best["job_id"]), "status": str(best["status"])}


# ── 조회 ────────────────────────────────────────────────────────────────────
# 목표에 연결된 진행·승인대기 작업. pipeline_jobs.goal_id 와 goal_task_links 양쪽을 본다.
GOAL_JOB_STATES_SQL = """
SELECT DISTINCT ON (x.goal_id, x.job_id)
       x.goal_id::text AS goal_id, x.milestone_id::text AS milestone_id,
       x.job_id, x.status, x.created_at
  FROM (
        SELECT j.goal_id, j.milestone_id, j.job_id, j.status, j.created_at
          FROM pipeline_jobs j
         WHERE j.goal_id = ANY($1::uuid[])
           AND j.status = ANY($2::text[])
        UNION ALL
        SELECT l.goal_id, j.milestone_id, j.job_id, j.status, j.created_at
          FROM goal_task_links l
          JOIN pipeline_jobs j ON j.job_id = l.task_id
         WHERE l.task_type = 'pipeline_job'
           AND l.goal_id = ANY($1::uuid[])
           AND j.status = ANY($2::text[])
       ) x
 ORDER BY x.goal_id, x.job_id
"""

# 연결된 채팅 세션에 최근 N분 안에 메시지가 있는 목표. 열려 있고 일시정지가 아닌 목표만.
GOAL_CHAT_ACTIVITY_SQL = """
SELECT DISTINCT l.goal_id::text AS goal_id
  FROM goal_task_links l
  JOIN goals g ON g.id = l.goal_id
  JOIN chat_messages m ON m.session_id::text = l.task_id
 WHERE l.task_type = 'chat_session'
   AND l.goal_id = ANY($1::uuid[])
   AND COALESCE(l.link_state, 'active') = 'active'
   AND g.status = ANY($3::text[])
   AND g.paused_at IS NULL
   AND m.created_at > now() - make_interval(mins => $2::int)
"""

UNLINKED_COUNT_SQL = """
SELECT count(*)::int
  FROM pipeline_jobs j
 WHERE j.chat_session_id = $1::uuid
   AND j.tenant_id = $2::uuid
   AND j.goal_id IS NULL
   AND j.created_at > now() - make_interval(hours => $3::int)
   AND COALESCE(j.status, '') <> 'dedup_blocked'
   AND COALESCE(j.phase, '') <> 'dedup_blocked'
   AND COALESCE(j.error_detail, '') NOT LIKE 'dedup_blocked%'
"""


async def fetch_job_states(conn: Any, goal_ids: Sequence[str]) -> list[dict[str, Any]]:
    ids = [str(g) for g in goal_ids if g]
    if not ids:
        return []
    rows = await conn.fetch(
        GOAL_JOB_STATES_SQL, ids, [*RUNNING_STATUSES, AWAITING_STATUS],
    )
    return [dict(r) for r in rows]


async def fetch_chat_active_goals(
    conn: Any, goal_ids: Sequence[str], *, minutes: int = CHAT_ACTIVE_MINUTES,
) -> set[str]:
    ids = [str(g) for g in goal_ids if g]
    if not ids:
        return set()
    rows = await conn.fetch(
        GOAL_CHAT_ACTIVITY_SQL, ids, int(minutes), list(_CHAT_OPEN_STATUSES),
    )
    return {str(r["goal_id"]) for r in rows}


async def unlinked_count(
    conn: Any, session_id: str, tenant_id: str, *, hours: int = UNLINKED_WINDOW_HOURS,
) -> int:
    try:
        return int(await conn.fetchval(UNLINKED_COUNT_SQL, session_id, tenant_id, int(hours)) or 0)
    except Exception as exc:  # noqa: BLE001 — 부가 신호가 목록을 막지 않는다
        logger.warning("goal_unlinked_count_failed: %s", str(exc)[:200])
        return 0


async def activity_for_goals(
    conn: Any, goal_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """{goal_id: {activity, runners: {milestone_id: current_runner}, goal_runner}}.

    조회가 실패해도 목록은 내려가야 하므로 실패 시 빈 활동으로 돌려준다.
    """
    ids = [str(g) for g in goal_ids if g]
    out: dict[str, dict[str, Any]] = {
        gid: {"activity": None, "runners": {}, "goal_runner": None} for gid in ids
    }
    if not ids:
        return out
    try:
        jobs = await fetch_job_states(conn, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("goal_job_state_lookup_failed: %s", str(exc)[:200])
        jobs = []
    try:
        chat_active = await fetch_chat_active_goals(conn, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("goal_chat_activity_lookup_failed: %s", str(exc)[:200])
        chat_active = set()

    by_goal: dict[str, list[dict[str, Any]]] = {}
    by_milestone: dict[tuple[str, str], list[dict[str, Any]]] = {}
    try:
        for job in jobs:
            gid = str(job["goal_id"])
            by_goal.setdefault(gid, []).append(job)
            if job.get("milestone_id"):
                by_milestone.setdefault((gid, str(job["milestone_id"])), []).append(job)
        chat_active = {str(g) for g in chat_active}
    except (KeyError, TypeError) as exc:  # 모양이 다른 행은 활동 없음으로 본다
        logger.warning("goal_activity_rows_malformed: %s", str(exc)[:200])
        by_goal, by_milestone = {}, {}

    for gid in ids:
        goal_jobs = by_goal.get(gid, [])
        statuses = {str(j.get("status")) for j in goal_jobs}
        out[gid]["activity"] = decide_activity(
            running=bool(statuses & set(RUNNING_STATUSES)),
            awaiting=AWAITING_STATUS in statuses,
            chat_active=gid in chat_active,
        )
        out[gid]["goal_runner"] = pick_current_runner(goal_jobs)
    for (gid, mid), milestone_jobs in by_milestone.items():
        runner = pick_current_runner(milestone_jobs)
        if runner:
            out[gid]["runners"][mid] = runner
    return out
