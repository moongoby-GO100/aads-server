"""목업 승인 후 다음 구현 단계 담당 채팅창에 안내 메시지를 1회 게시한다.

CEO 결정: 구현 작업을 자동 제출하지 않는다(승인본과 다른 지시서가 만들어질 위험). 안내만 한다.
AI 턴을 띄우지 않는다 — session_reporter.post_session_report 는 chat_messages 저장만 하며
trigger_reaction=False 로 호출한다(LLM 비용 0, 자동 제출 없음).

승인 트랜잭션이 커밋된 뒤(router) 호출된다. 어떤 예외도 밖으로 내보내지 않는다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.core.db_pool import get_pool
from app.services import goal_manager
from app.services.mockup_review_service import _eligible, _workspace_in_scope, notification_link
from app.services.session_reporter import normalize_session_id, post_session_report

logger = logging.getLogger(__name__)

NO_OWNER_NOTE = "목업 승인 후 안내할 담당 없음"
_FAILED_LINK_STATUSES = "('failed', 'error', 'rejected', 'rejected_done')"
_FAILED_JOB_STATUSES = "('failed', 'error', 'rejected', 'rejected_done', 'cancelled')"


@dataclass(frozen=True)
class HandoffTarget:
    milestone_id: str
    milestone_title: str
    session_id: str | None  # None = 안내할 담당 없음


def marker(event_id: Any) -> str:
    return f"[mockup-approved:{event_id}]"


# 마일스톤에는 목업·구현 단계를 가리키는 구조 필드가 없다. 제목으로만 판정하고, 애매하면 고르지 않는다.
_IMPL_RE = re.compile(r"구현|개발|코딩|implement|develop|coding", re.I)
_MOCKUP_RE = re.compile(r"목업|시안|와이어프레임|mockup|wireframe", re.I)


def is_implementation_stage(title: Any) -> bool:
    return bool(_IMPL_RE.search(str(title or "")))


def pick_next_milestone(rows: list[Any]) -> Any:
    """명시적인 승인-마일스톤 연결이 없는 다중 목업 목표는 fail closed 한다.

    현재 review 구조에는 goal_id만 있고 milestone_id 연결이 없다. 그러므로 한 목업
    경계일 때만 그 뒤의 첫 미연결 단계로 진행하며, 복수 목업에서는 추정하지 않는다.
    """
    ordered = sorted(rows, key=lambda r: (r["sequence_order"], r["id"]))
    mockup_rows = [r for r in ordered if _MOCKUP_RE.search(r["title"] or "")]
    if not mockup_rows:
        return None
    if len(mockup_rows) != 1:
        return None
    boundary = mockup_rows[0]["sequence_order"]
    for r in ordered:
        if r["status"] not in ("in_progress", "pending") or r["has_progress"]:
            continue
        if r["sequence_order"] <= boundary:
            continue
        return r
    return None


async def _next_milestone(conn: Any, tenant_id: Any, goal_id: Any, project: Any) -> Any:
    """연결 판정(has_progress)은 goal_dispatch 와 같다. 단계 판정은 pick_next_milestone."""
    columns = await goal_manager.link_optional_columns(conn)
    rows = await conn.fetch(
        f"""
        SELECT m.id::text AS id, m.title, m.status, m.sequence_order,
               m.owner_session_id::text AS owner_session_id, m.owner_role_key,
               EXISTS (
                 SELECT 1 FROM goal_task_links l
                  WHERE l.milestone_id = m.id
                    AND l.task_type = 'pipeline_job'
                    {goal_manager.active_link_predicate(columns, "l")}
                    {goal_manager.superseded_link_predicate(columns, "l")}
                    AND COALESCE(l.status, 'pending') NOT IN {_FAILED_LINK_STATUSES}
                    AND NOT EXISTS (
                          SELECT 1 FROM pipeline_jobs j
                           WHERE j.job_id = l.task_id AND j.status IN {_FAILED_JOB_STATUSES})) AS has_progress
          FROM milestones m JOIN goals g ON g.id = m.goal_id
         WHERE m.goal_id = $1::uuid AND m.tenant_id = $2::uuid AND m.project = $3
           AND g.tenant_id = $2::uuid AND g.project = $3
         ORDER BY m.sequence_order, m.id
        """,
        str(goal_id), str(tenant_id), str(project))
    rows = list(rows)
    mockup_count = sum(bool(_MOCKUP_RE.search(r["title"] or "")) for r in rows)
    if mockup_count > 1:
        logger.info("mockup_approval_handoff no_target reason=ambiguous_mockup_boundary goal=%s",
                    str(goal_id)[:8])
        return None
    if not mockup_count:
        logger.info("mockup_approval_handoff no_target reason=no_mockup_stage goal=%s", str(goal_id)[:8])
    return pick_next_milestone(rows)


async def _usable_session(conn: Any, tenant_id: Any, project: str, session_id: Any) -> str | None:
    """같은 tenant·같은 프로젝트 작업공간의 세션이고, 그 소유자가 이 목업을 볼 권한이 있을 때만 쓴다.

    mockup_review_service._session_scope / _eligible 과 같은 기준이다 — 안내문에는 제목·식별자·링크가 들어간다.
    """
    sid = normalize_session_id(session_id)
    if not sid:
        return None
    row = await conn.fetchrow(
        "SELECT s.id::text AS id, s.user_id, w.project_key, w.name AS workspace_name, "
        "w.display_name AS workspace_display_name "
        "FROM chat_sessions s JOIN chat_workspaces w ON w.id = s.workspace_id "
        "WHERE s.id = $1::uuid AND s.tenant_id = $2::uuid AND w.tenant_id = $2::uuid", sid, str(tenant_id))
    if row is None or not _workspace_in_scope(
            row["project_key"], (row["workspace_name"], row["workspace_display_name"]), project):
        return None
    owner = row["user_id"]
    if owner and not await _eligible(conn, str(tenant_id), project, [owner], ("read", "write", "approve"), owner):
        return None
    return str(row["id"])


async def _role_session(conn: Any, tenant_id: Any, project: str, role_key: Any) -> str | None:
    role = str(role_key or "").strip()
    if not role:
        return None
    # goal_dispatch.py 의 LATERAL 정렬(최근 갱신 1개)을 따르되, 이 프로젝트 작업공간의 세션만 후보로 둔다.
    rows = await conn.fetch(
        """
        SELECT cs.id::text AS id, w.project_key, w.name AS workspace_name, w.display_name AS workspace_display_name
          FROM chat_sessions cs JOIN chat_workspaces w ON w.id = cs.workspace_id
         WHERE cs.role_key = $1 AND cs.tenant_id = $2::uuid AND w.tenant_id = $2::uuid
         ORDER BY cs.updated_at DESC NULLS LAST, cs.created_at DESC, cs.id
         LIMIT 50
        """,
        role, str(tenant_id))
    for r in rows:
        if _workspace_in_scope(r["project_key"], (r["workspace_name"], r["workspace_display_name"]), project):
            session_id = await _usable_session(conn, tenant_id, project, r["id"])
            if session_id is not None:
                return session_id
    return None


async def select_handoff_target(conn: Any, tenant_id: Any, goal_id: Any, project: str,
                                head_session_id: Any = None) -> HandoffTarget | None:
    """조회 전용. 대상 마일스톤이 없으면 None, 있으나 담당 세션이 없으면 session_id=None."""
    if not goal_id:
        return None
    milestone = await _next_milestone(conn, tenant_id, goal_id, project)
    if milestone is None:
        return None
    session_id = await _usable_session(conn, tenant_id, project, milestone["owner_session_id"])
    if session_id is None:
        session_id = await _role_session(conn, tenant_id, project, milestone["owner_role_key"])
    if session_id is None:
        lead = await conn.fetchval(
            "SELECT owner_session_id::text FROM goals WHERE id = $1::uuid AND tenant_id = $2::uuid",
            str(goal_id), str(tenant_id))
        session_id = await _usable_session(conn, tenant_id, project, lead)
    if session_id is None:
        session_id = await _usable_session(conn, tenant_id, project, head_session_id)
    return HandoffTarget(milestone["id"], milestone["title"] or "", session_id)


def _one_line(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def build_message(*, title: str, revision: Any, milestone: HandoffTarget, review_id: Any, revision_id: Any,
                  link: str, event_id: Any) -> str:
    rev = f" r{revision}" if revision else ""
    return (
        f"🖼 [목업 승인] '{_one_line(title, 120)}'{rev} 승인됨 — 다음 단계 「{_one_line(milestone.milestone_title, 120)}」에 "
        "연결된 구현 작업이 없습니다. 승인본 기준으로 구현 지시서를 제출하려면 이 대화에서 지시해 주세요. "
        "(자동 제출하지 않음)\n\n"
        f"- review_id: {review_id}\n- revision_id: {revision_id}\n- milestone_id: {milestone.milestone_id}\n"
        f"- 승인본 열기: {link}\n\n{marker(event_id)}"
    )


async def _already_posted(conn: Any, session_id: str, event_id: Any) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM chat_messages WHERE session_id = $1::uuid AND position($2 in content) > 0 LIMIT 1",
        session_id, marker(event_id)))


async def _note_no_owner(conn: Any, milestone_id: str) -> None:
    await conn.execute(
        "UPDATE milestones SET dispatch_note = $2, updated_at = NOW() "
        " WHERE id = $1::uuid AND dispatch_note IS DISTINCT FROM $2",
        milestone_id, NO_OWNER_NOTE)


async def announce_approved_review(review_id: Any, approval_event_id: Any) -> bool:
    """승인 커밋 이후 호출. 게시했으면 True. 예외는 모두 삼키고 warning 만 남긴다."""
    try:
        async with get_pool().acquire() as conn:
            # Keep the review row locked through target selection and message insert. A concurrent
            # revoke/re-approval must wait, so an obsolete approval can never publish mid-selection.
            async with conn.transaction():
                head = await conn.fetchrow(
                    "SELECT h.id, h.tenant_id, h.project_key, h.title, h.goal_id, h.session_id, h.status, "
                    "h.approval_event_id, h.approved_revision_id, r.revision "
                    "FROM mockup_review_heads h LEFT JOIN mockup_review_revisions r ON r.id = h.approved_revision_id "
                    "WHERE h.id = $1::uuid FOR SHARE OF h", str(review_id))
                if head is None or not head["goal_id"]:
                    return False
                # Replays and stale approval events do not send a message.
                if head["status"] != "approved" or head["approval_event_id"] != approval_event_id:
                    return False
                target = await select_handoff_target(conn, head["tenant_id"], head["goal_id"], head["project_key"],
                                                     head["session_id"])
                if target is None:
                    logger.info("mockup_approval_handoff no_target reason=no_candidate review=%s", str(review_id)[:8])
                    return False
                if target.session_id is None:
                    await _note_no_owner(conn, target.milestone_id)
                    logger.info("mockup_approval_handoff no_target reason=no_owner review=%s milestone=%s",
                                str(review_id)[:8], target.milestone_id[:8])
                    return False
                if await _already_posted(conn, target.session_id, approval_event_id):
                    return False
                link = notification_link(head["project_key"], head["id"], head["approved_revision_id"],
                                         target.session_id)
                result = await post_session_report(
                    session_id=target.session_id,
                    title="목업 승인 — 다음 단계 안내",
                    body=build_message(title=head["title"], revision=head["revision"], milestone=target,
                                       review_id=head["id"], revision_id=head["approved_revision_id"], link=link,
                                       event_id=approval_event_id),
                    status="warning",
                    source="mockup_approval_handoff",
                    project=head["project_key"],
                    idempotency_key=f"mockup-approved:{approval_event_id}",
                    trigger_reaction=False,
                    plain_body=True,
                    conn=conn,
                )
                if not result.posted:
                    logger.warning("mockup_approval_handoff skipped review=%s reason=%s",
                                   str(review_id)[:8], result.skipped_reason)
                return bool(result.posted)
    except Exception:  # noqa: BLE001 - 승인 결과는 이미 커밋됐다. 안내 실패가 응답을 깨면 안 된다.
        logger.warning("mockup_approval_handoff failed review=%s", review_id, exc_info=True)
        return False
