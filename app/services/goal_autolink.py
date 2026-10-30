"""러너 제출 시 목표 자동 연결 (PRD goal-priority-dormancy-autoapproval-display-prd §6-1 I-3).

연결 근거 순서 — 앞 근거가 **있으면** 뒤로 내려가지 않는다.

    (a) 제출 인자의 goal_id/milestone_id                     → source ``explicit``
    (b) 지시문의 ``GOAL_ID:``/``MILESTONE_ID:`` 줄             → source ``directive``
    (c) 제출 세션이 주도·담당한 열린 목표의 in_progress 마일스톤이
        **정확히 1개**                                       → source ``session_single``
    (d) 그 밖(후보 0개·2개 이상·세션 불명)                     → source ``none`` (비워 둔다)

추정 연결 금지(I-2 B). (c) 는 추정이 아니라 "이 세션이 맡은 단 하나의 진행 중 마일스톤"
이라는 결정적 사실에만 붙는다. 둘 이상이면 어느 쪽인지 모르므로 붙이지 않는다.

(a)(b) 의 검증(목표·마일스톤 실재, 같은 tenant/project, 열린 상태)과 goal_task_links 기록은
`pipeline_runner_service._link_job_to_goal_explicit` 하나만 한다 — 규칙을 두 벌로 두지 않는다.
(c) 도 같은 함수를 거쳐 같은 검증을 받고 bind_source 만 ``session_single`` 로 남는다.

- 존재하지 않는 UUID 는 **무시**하고 다음 근거로 내려간다.
- 목표는 실재하지만 정책상 거부된 경우(다른 프로젝트·종료·차단 목표 등)는 거기서 멈춘다.
  지시문이 가리킨 목표를 버리고 다른 목표에 붙이는 것은 지시를 뒤집는 추정이다.

연결 출처는 pipeline_jobs.logs 의 ``goal_autolink`` 이벤트와 goal_task_links.bind_source 에
남는다(pipeline_jobs 에는 metadata 칸이 없어 마이그레이션 없이 기존 logs 를 쓴다).
"""
from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional, Sequence

from app.services.goal_binding import (
    BIND_SOURCE_EXPLICIT_API,
    BIND_SOURCE_EXPLICIT_DIRECTIVE,
    BIND_SOURCE_SESSION_SINGLE,
    is_uuid,
    parse_goal_binding,
)

logger = logging.getLogger(__name__)

SOURCE_EXPLICIT = "explicit"
SOURCE_DIRECTIVE = "directive"
SOURCE_SESSION_SINGLE = "session_single"
SOURCE_NONE = "none"

_SOURCE_BY_BIND_SOURCE = {
    BIND_SOURCE_EXPLICIT_API: SOURCE_EXPLICIT,
    BIND_SOURCE_EXPLICIT_DIRECTIVE: SOURCE_DIRECTIVE,
    BIND_SOURCE_SESSION_SINGLE: SOURCE_SESSION_SINGLE,
}

REASON_NO_CANDIDATE = "no_candidate"
REASON_AMBIGUOUS = "ambiguous"
REASON_NO_SESSION = "no_session"
REASON_EXPLICIT_REJECTED = "explicit_rejected"
REASON_CANDIDATE_REJECTED = "candidate_rejected"
REASON_ERROR = "error"


class AutolinkResult(NamedTuple):
    goal_id: Optional[str]
    milestone_id: Optional[str]
    source: str
    reason: Optional[str] = None

    @property
    def linked(self) -> bool:
        return bool(self.goal_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "milestone_id": self.milestone_id,
            "source": self.source,
            **({"reason": self.reason} if self.reason else {}),
        }


def _none(reason: str) -> AutolinkResult:
    return AutolinkResult(None, None, SOURCE_NONE, reason)


def source_of_bind_source(bind_source: Optional[str]) -> str:
    return _SOURCE_BY_BIND_SOURCE.get(bind_source or "", SOURCE_NONE)


# 세션이 주도(goals.owner_session_id)하거나 담당(마일스톤 owner)하는, 열린 목표의 in_progress 마일스톤.
# 주도는 목표 전체를, 담당은 자기 몫만 진다 — GET /goals/for-session 과 같은 해석이다.
# 일시정지 목표·종료 목표·차단 목표에는 새 일을 자동으로 얹지 않는다(draft/active 만).
# Deliberately keep implicit session candidates scoped to the job project;
# cross-project links are allowed only through explicit runner goal metadata.
SESSION_CANDIDATES_SQL = """
SELECT DISTINCT ON (ms.id) g.id::text AS goal_id, ms.id::text AS milestone_id
  FROM goal_task_links l
  JOIN chat_sessions s ON s.id::text = l.task_id AND s.tenant_id = $2::uuid
  JOIN goals g ON g.id = l.goal_id AND g.tenant_id = $2::uuid
  JOIN milestones ms ON ms.goal_id = g.id AND ms.tenant_id = $2::uuid
 WHERE l.task_type = 'chat_session'
   AND l.task_id = $1
   AND COALESCE(l.link_state, 'active') = 'active'
   AND g.status IN ('draft', 'active')
   AND g.paused_at IS NULL
   AND g.project = $3
   AND ms.project = $3
   AND ms.status = 'in_progress'
   AND (g.owner_session_id = s.id
        OR ms.owner_session_id = s.id
        OR (ms.owner_session_id IS NULL AND s.role_key IS NOT NULL
            AND ms.owner_role_key = s.role_key))
 ORDER BY ms.id
 LIMIT 2
"""


def pick_single(candidates: Sequence[Any]) -> Optional[tuple[str, str]]:
    """후보가 서로 다른 마일스톤 정확히 1개일 때만 (goal_id, milestone_id). 0개·2개 이상은 None."""
    unique: dict[str, str] = {}
    for row in candidates or ():
        goal = str(row["goal_id"] or "")
        milestone = str(row["milestone_id"] or "")
        if goal and milestone:
            unique[milestone] = goal
    if len(unique) != 1:
        return None
    milestone, goal = next(iter(unique.items()))
    return goal, milestone


async def _goal_exists(goal_id: str, tenant_id: Optional[str]) -> bool:
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT 1 FROM goals WHERE id = $1::uuid AND ($2::uuid IS NULL OR tenant_id = $2::uuid)",
            goal_id, tenant_id,
        ))


async def _session_candidates(
    session_id: str, tenant_id: str, project: str,
) -> list[Any]:
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        return list(await conn.fetch(SESSION_CANDIDATES_SQL, session_id, tenant_id, project))


async def _record(job_id: str, result: AutolinkResult) -> None:
    """연결 출처를 pipeline_jobs.logs 에 남긴다 (best-effort — 제출을 막지 않는다)."""
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE pipeline_jobs
                   SET logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                         'ts', NOW()::text, 'event', 'goal_autolink',
                         'source', $2::text, 'reason', $3::text,
                         'goal_id', $4::text, 'milestone_id', $5::text))
                 WHERE job_id = $1
                   AND (logs IS NULL OR jsonb_typeof(logs) = 'array')
                """,
                job_id, result.source, result.reason or "",
                result.goal_id or "", result.milestone_id or "",
            )
    except Exception as exc:  # noqa: BLE001 — 출처 기록 실패가 제출을 막지 않는다
        logger.debug("goal_autolink_record_failed job=%s: %s", job_id, str(exc)[:200])


async def autolink_job(
    job_id: str,
    project: str,
    *,
    instruction: Optional[str] = None,
    goal_id: Optional[str] = None,
    milestone_id: Optional[str] = None,
    session_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> AutolinkResult:
    """이미 INSERT 되어 **커밋된** 작업을 (a)>(b)>(c) 순서로 목표에 연결한다.

    반드시 작업 행이 다른 연결에서 보이는 상태(커밋 뒤)에서 불러라 — 연결 함수가 별도 DB
    연결로 `pipeline_jobs` 행을 잠그기 때문에, 열린 트랜잭션 안에서 부르면 행이 안 보여
    조용히 미연결이 된다(배치 제출이 그랬다).
    """
    from app.services import pipeline_runner_service as prs

    try:
        result = await _resolve_and_link(
            prs, job_id, project, instruction=instruction, goal_id=goal_id,
            milestone_id=milestone_id, session_id=session_id, tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 — 연결 실패는 제출 실패가 아니다
        logger.warning("goal_autolink_failed job=%s: %s", job_id, str(exc)[:200])
        result = _none(REASON_ERROR)
    try:
        if result.linked:
            from app.core.db_pool import get_pool
            from app.services.goal_work_item_sync import bind_work_items

            async with get_pool().acquire() as conn:
                async with conn.transaction():
                    await bind_work_items(conn, job_id, instruction)
    except Exception as exc:
        logger.warning("work_item_bind_failed job=%s: %s", job_id, str(exc)[:200])
    await _record(job_id, result)
    logger.info(
        "goal_autolink job=%s source=%s goal=%s milestone=%s reason=%s",
        job_id, result.source, result.goal_id, result.milestone_id, result.reason,
    )
    return result


async def _resolve_and_link(
    prs: Any, job_id: str, project: str, *, instruction: Optional[str],
    goal_id: Optional[str], milestone_id: Optional[str],
    session_id: Optional[str], tenant_id: Optional[str],
) -> AutolinkResult:
    # (a) 제출 인자
    arg_binding = parse_goal_binding(None, goal_id=goal_id, milestone_id=milestone_id)
    if arg_binding.is_explicit:
        link = await prs._link_job_to_goal_explicit(
            job_id, project, instruction=instruction,
            goal_id=goal_id or None, milestone_id=milestone_id or None,
        )
        if link:
            return AutolinkResult(link.goal_id, link.milestone_id, SOURCE_EXPLICIT)
        if await _goal_exists(arg_binding.goal_id, tenant_id):
            return _none(REASON_EXPLICIT_REJECTED)

    # (b) 지시문 줄
    directive_binding = parse_goal_binding(instruction)
    if directive_binding.is_explicit:
        link = await prs._link_job_to_goal_explicit(job_id, project, instruction=instruction)
        if link:
            return AutolinkResult(link.goal_id, link.milestone_id, SOURCE_DIRECTIVE)
        if await _goal_exists(directive_binding.goal_id, tenant_id):
            return _none(REASON_EXPLICIT_REJECTED)

    # (c) 제출 세션의 단일 in_progress 마일스톤
    if not (session_id and tenant_id and is_uuid(session_id)):
        return _none(REASON_NO_SESSION)
    candidates = await _session_candidates(session_id, tenant_id, project)
    picked = pick_single(candidates)
    if picked is None:
        return _none(REASON_AMBIGUOUS if len(candidates) > 1 else REASON_NO_CANDIDATE)
    link = await prs._link_job_to_goal_explicit(
        job_id, project, goal_id=picked[0], milestone_id=picked[1],
        bind_source=BIND_SOURCE_SESSION_SINGLE,
    )
    if not link:
        return _none(REASON_CANDIDATE_REJECTED)
    return AutolinkResult(link.goal_id, link.milestone_id, SOURCE_SESSION_SINGLE)
