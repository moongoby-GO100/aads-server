"""목표·마일스톤 진행 상태(progress_state) 판정.

목표띠의 "🔒 수동"은 승인 정책이지 실행 방식이 아니다. 실제로 멈춘 사유는
`milestones` 의 발송 기록(dispatched_at / dispatch_count / dispatch_blocked_at /
dispatch_note)에 있는데 화면에는 구조화된 값으로 내려가지 않았다. 이 모듈이
그 기록을 한 값으로 접는다.

판정은 DB 컬럼이 근거다. `dispatch_note` 문구는 `goal_dispatch` 가 남기는 표식이
있을 때만 보조로 쓴다(`ANSWERED_NO_PROGRESS_MARK`, `goal_dispatch._SUBSTITUTE_NOTE`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.services import goal_dispatch, goal_manager

logger = logging.getLogger(__name__)

WORKING = "working"
NO_OWNER = "no_owner"
AWAITING_REPLY = "awaiting_reply"
ANSWERED_NO_PROGRESS = "answered_no_progress"
BLOCKED = "blocked"
PAUSED = "paused"
IDLE = "idle"

# 목표 대표값을 고를 때의 심각도. 앞일수록 심각하다. paused 는 목표 멈춤일 때만 맨 위로 간다.
SEVERITY_ORDER = (
    BLOCKED, ANSWERED_NO_PROGRESS, NO_OWNER, AWAITING_REPLY, WORKING, PAUSED, IDLE,
)

# goal_dispatch 가 "답은 왔으나 진행 없음" 으로 막을 때 dispatch_note 앞머리에 쓰는 문구.
ANSWERED_NO_PROGRESS_MARK = "담당이 답했으나 진행"
SUBSTITUTE_NOTE = goal_dispatch._SUBSTITUTE_NOTE


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def classify_milestone(
    m: Mapping[str, Any], *, goal_paused: bool = False, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """진행중 마일스톤 하나의 상태. `{state, reason, substitute}` 를 돌려준다.

    위에서부터 처음 맞는 것:
    멈춤 > 살아 있는 러너(막힘 기록이 낡았어도 진행중) > 막힘/답은 왔으나 진행 없음
    > 담당 없음 > 발송 후(답 대기 / 답 확인) > 대기.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    note = str(m.get("dispatch_note") or "").strip()
    count = int(m.get("dispatch_count") or 0)
    dispatched_at = _aware(m.get("dispatched_at"))
    blocked_at = m.get("dispatch_blocked_at")
    answered_at = _aware(m.get("answered_at"))
    lead = str(m.get("goal_lead_session_id") or "").strip()
    owner_resolved = bool(m.get("owner_resolved"))
    substitute = (not owner_resolved) and bool(lead) and (
        note == SUBSTITUTE_NOTE or str(m.get("dispatched_session_id") or "") == lead
    )

    def out(state: str, reason: str) -> dict[str, Any]:
        return {"state": state, "reason": reason, "substitute": substitute and state == NO_OWNER}

    if goal_paused:
        return out(PAUSED, "목표가 멈춰 있음")
    if m.get("owner_paused"):
        return out(PAUSED, "담당이 멈춰 있음")
    if m.get("live_runner"):
        return out(WORKING, "러너 작업 진행 중")

    answered = answered_at is not None or note.startswith(ANSWERED_NO_PROGRESS_MARK)
    if blocked_at is not None:
        if answered:
            return out(ANSWERED_NO_PROGRESS, note or "담당이 답했으나 진행(러너·완료 보고)이 없음")
        return out(BLOCKED, note or f"{goal_dispatch._MAX_DISPATCH}회 보냈으나 답이 확인되지 않음")

    if not owner_resolved:
        if substitute:
            return out(NO_OWNER, "담당 세션 없음 — 주도 세션으로 대체 발송")
        return out(NO_OWNER, note or "담당 세션 없음")

    if dispatched_at is not None:
        if answered_at is None:
            return out(AWAITING_REPLY, f"{count}회 보냄 — 답 대기")
        grace = timedelta(minutes=goal_dispatch._PROGRESS_GRACE_MIN)
        if answered_at > now - grace:
            return out(WORKING, "담당이 방금 답함")
        return out(ANSWERED_NO_PROGRESS, "담당이 답했으나 진행(러너·완료 보고)이 없음")

    return out(IDLE, "아직 지시를 보내지 않음")


def summarize_goal(
    milestones: Sequence[Mapping[str, Any]],
    *, goal_paused: bool = False, goal_paused_reason: Optional[str] = None,
) -> dict[str, Any]:
    """분류된 마일스톤들(`state`/`reason`/`substitute` + id·title 포함)에서 목표 대표값을 만든다."""
    blocked = [
        {
            "milestone_id": m.get("milestone_id"),
            "title": m.get("title"),
            "state": m["state"],
            "reason": m.get("dispatch_note") or m["reason"],
            "blocked_at": _iso(m.get("dispatch_blocked_at")),
        }
        for m in milestones
        if m.get("dispatch_blocked_at") is not None and m["state"] in (BLOCKED, ANSWERED_NO_PROGRESS)
    ]
    if goal_paused:
        reason = "목표가 멈춰 있음" + (f" — {goal_paused_reason}" if goal_paused_reason else "")
        return {
            "progress_state": PAUSED, "progress_reason": reason,
            "progress_substitute": False, "blocked_milestones": blocked,
        }
    if not milestones:
        return {
            "progress_state": IDLE, "progress_reason": "진행 중인 마일스톤 없음",
            "progress_substitute": False, "blocked_milestones": [],
        }
    rep = min(milestones, key=lambda m: SEVERITY_ORDER.index(m["state"]))
    title = rep.get("title")
    reason = f"{title}: {rep['reason']}" if title else rep["reason"]
    return {
        "progress_state": rep["state"], "progress_reason": reason,
        "progress_substitute": bool(rep.get("substitute")), "blocked_milestones": blocked,
    }


def empty_progress() -> dict[str, Any]:
    """판정을 못 했을 때의 값. 키는 항상 내려가야 화면이 분기하지 않는다."""
    return {
        "progress_state": None, "progress_reason": None,
        "progress_substitute": False, "blocked_milestones": [], "milestones": {},
    }


_GOALS_SQL = """
SELECT id::text AS goal_id, paused_at, paused_reason
  FROM goals WHERE id = ANY($1::uuid[]) AND tenant_id = $2::uuid
"""

_MILESTONES_SQL = """
SELECT m.id::text AS milestone_id, m.goal_id::text AS goal_id, m.title,
       m.dispatched_at, COALESCE(m.dispatch_count, 0) AS dispatch_count,
       m.dispatch_blocked_at, m.dispatch_note,
       m.dispatched_session_id::text AS dispatched_session_id,
       COALESCE(g.owner_session_id::text, '') AS goal_lead_session_id,
       (m.owner_session_id IS NOT NULL OR (
            m.owner_role_key IS NOT NULL AND EXISTS (
                SELECT 1 FROM chat_sessions cs
                 WHERE cs.tenant_id = $2::uuid AND cs.role_key = m.owner_role_key))
       ) AS owner_resolved,
       EXISTS (
            SELECT 1 FROM owner_pause op
             WHERE op.goal_id = m.goal_id
               AND (op.session_id = m.owner_session_id
                    OR (m.owner_session_id IS NULL AND op.session_id IN (
                        SELECT cs.id FROM chat_sessions cs
                         WHERE cs.tenant_id = $2::uuid AND cs.role_key = m.owner_role_key)))
       ) AS owner_paused,
       EXISTS (
            SELECT 1 FROM goal_task_links l
             WHERE l.milestone_id = m.id AND l.task_type = 'pipeline_job'
               {link_predicates}
               AND l.status NOT IN ('failed', 'error', 'rejected', 'rejected_done')
               AND NOT EXISTS (
                   SELECT 1 FROM pipeline_jobs j
                    WHERE j.job_id = l.task_id
                      AND j.status IN ('failed', 'error', 'rejected', 'rejected_done', 'cancelled'))
       ) AS live_runner,
       (SELECT MIN(c.created_at) FROM chat_messages c
         WHERE m.dispatched_at IS NOT NULL
           AND c.session_id = COALESCE(m.dispatched_session_id, m.owner_session_id)
           AND c.role = 'assistant' AND c.created_at > m.dispatched_at
           AND length(c.content) > 40
           AND c.content NOT LIKE '⏳%'
           AND c.content NOT LIKE '⚠️ _응답 생성이%'
       ) AS answered_at
  FROM milestones m
  JOIN goals g ON g.id = m.goal_id
 WHERE m.goal_id = ANY($1::uuid[]) AND m.tenant_id = $2::uuid
   AND m.status = 'in_progress'
 ORDER BY m.sequence_order, COALESCE(m.variant, '')
"""


async def progress_for_goals(
    conn: Any, goal_ids: Iterable[str], tenant_id: str, *, now: Optional[datetime] = None,
) -> dict[str, dict[str, Any]]:
    """{goal_id: {progress_state, progress_reason, progress_substitute, blocked_milestones,
    milestones: {milestone_id: {progress_state, progress_reason, substitute}}}}.

    조회가 실패해도 목록은 내려가야 하므로 실패 시 빈 값(`empty_progress`)으로 돌려준다.
    """
    ids = [str(g) for g in goal_ids if g]
    out = {gid: empty_progress() for gid in ids}
    if not ids:
        return out
    try:
        columns = await goal_manager.link_optional_columns(conn)
        predicates = (
            goal_manager.active_link_predicate(columns, "l")
            + goal_manager.superseded_link_predicate(columns, "l")
        )
        goal_rows = await conn.fetch(_GOALS_SQL, ids, tenant_id)
        ms_rows = await conn.fetch(_MILESTONES_SQL.format(link_predicates=predicates), ids, tenant_id)
    except Exception as exc:  # noqa: BLE001 — 부가 신호가 응답을 막지 않는다
        logger.warning("goal_progress_state_lookup_failed: %s", str(exc)[:200])
        return out

    goals = {str(r["goal_id"]): r for r in goal_rows}
    by_goal: dict[str, list[dict[str, Any]]] = {}
    for row in ms_rows:
        by_goal.setdefault(str(row["goal_id"]), []).append(dict(row))

    for gid in ids:
        goal = goals.get(gid)
        if goal is None:
            continue
        paused = goal["paused_at"] is not None
        classified = []
        for m in by_goal.get(gid, []):
            c = classify_milestone(m, goal_paused=paused, now=now)
            classified.append({**m, **c})
        summary = summarize_goal(
            classified, goal_paused=paused, goal_paused_reason=goal["paused_reason"],
        )
        summary["milestones"] = {
            m["milestone_id"]: {
                "progress_state": m["state"], "progress_reason": m["reason"],
                "substitute": m["substitute"],
            }
            for m in classified
        }
        out[gid] = summary
    return out
