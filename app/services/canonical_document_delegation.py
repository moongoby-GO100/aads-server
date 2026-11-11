"""정본 문서 승인 위임 — 부여·철회·유효성 조회와 위임 승인 조건 판정.

사람이 프로젝트(선택: goal) 단위로 위임을 만들면, 그 grantee(채팅 세션 id 또는 role_key)가
교차검수를 통과한 최신 판본만 승인할 수 있다. 승인 실행은 canonical_documents.approve_revision_in_tx 한 곳이고,
사람 승인(/approve)과 doc_auto_approval 의 판정 기준은 여기서 바꾸지 않는다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.services import doc_cross_review as cross
from app.services import doc_auto_approval as auto

DEFAULT_EXPIRES_DAYS = 30
MAX_EXPIRES_DAYS = 365
REQUIRED_PROVIDERS = auto.REQUIRED_PROVIDERS


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _denied(code: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, **extra}


async def create_delegation(conn: Any, tenant: str, project: str, grantee: str, granted_by: str,
                            goal_id: Any = None, expires_days: int = DEFAULT_EXPIRES_DAYS) -> dict[str, Any]:
    days = max(1, min(int(expires_days), MAX_EXPIRES_DAYS))
    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    row = await conn.fetchrow(
        "INSERT INTO project_document_approval_delegations"
        "(tenant_id,project_key,goal_id,grantee,granted_by,expires_at) "
        "VALUES($1::uuid,$2,$3,$4,$5,$6) RETURNING id,created_at,expires_at",
        tenant, project, goal_id, grantee.strip(), granted_by, expires_at)
    return {"delegation_id": str(row["id"]), "project": project, "goal_id": str(goal_id) if goal_id else None,
            "grantee": grantee.strip(), "granted_by": granted_by, "expires_at": row["expires_at"].isoformat()}


async def revoke_delegation(conn: Any, tenant: str, project: str, delegation_id: Any, revoked_by: str) -> bool:
    locked = await conn.fetchrow(
        "SELECT * FROM project_document_approval_delegations "
        "WHERE id=$1 AND tenant_id=$2::uuid AND project_key=$3 FOR UPDATE",
        delegation_id, tenant, project)
    if not locked or locked["revoked_at"] is not None:
        return False
    row = await conn.fetchrow(
        "UPDATE project_document_approval_delegations SET revoked_at=now(),revoked_by=$4 "
        "WHERE id=$1 AND tenant_id=$2::uuid AND project_key=$3 AND revoked_at IS NULL RETURNING id",
        delegation_id, tenant, project, revoked_by)
    return row is not None


def cross_review_problem(rows: Any) -> Optional[str]:
    """두 회사 모두 최신 verdict=pass, reject·blocker·major 0 이면 None, 아니면 막은 코드."""
    latest = cross._latest_by_model(list(rows or []))
    if any(r.get("verdict") == "reject" for r in latest.values()):
        return "cross_review_reject_present"
    passed = {r.get("reviewer_provider") for r in latest.values() if r.get("verdict") == "pass"}
    if not REQUIRED_PROVIDERS <= passed:
        return "cross_review_incomplete"
    # 구조 데이터가 없는(옛 형식) 리뷰·잘린 리뷰·비유한 값은 지적 집계를 믿을 수 없으므로 통과로 보지 않는다.
    for row in latest.values():
        data, problem = auto.review_problem(row)
        if (problem in ("invalid", "legacy", "truncated", "non_finite")
                or not isinstance((data or {}).get("issues"), list)):
            return "cross_review_unreliable"
    counts = auto._issue_counts(latest, frozenset())
    if counts["blocker"]:
        return "cross_review_blocker_present"
    if counts["major"]:
        return "cross_review_major_present"
    return None


def _session_matches(row: Any, session_id: str, session_role: str) -> bool:
    grantee = str(row["grantee"])
    return grantee == session_id or (bool(_norm(grantee)) and _norm(grantee) == _norm(session_role))


async def evaluate_delegated_approval(conn: Any, tenant: str, project: str, head: Any, revision_id: Any,
                                      expected_generation: int, session_id: str,
                                      session_role: str = "", *, lock_delegation: bool = False) -> dict[str, Any]:
    """조회는 읽기 전용. 실행 시 head 잠금 트랜잭션에서 lock_delegation=True 로 호출한다."""
    if str(head["approved_revision_id"]) == str(revision_id):
        return _denied("already_approved")
    if str(head["latest_revision_id"]) != str(revision_id):
        return _denied("not_latest_revision")
    if head["generation"] != expected_generation:
        return _denied("generation_conflict")

    goal_ids = [r["goal_id"] for r in await conn.fetch(
        "SELECT goal_id FROM project_document_goal_links WHERE head_id=$1", head["id"])]
    delegations = await conn.fetch(
        "SELECT * FROM project_document_approval_delegations WHERE tenant_id=$1::uuid AND project_key=$2 "
        "AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC", tenant, project)
    if not delegations:
        return _denied("delegation_missing")
    in_scope = [d for d in delegations if d["goal_id"] is None or d["goal_id"] in goal_ids]
    if not in_scope:
        return _denied("goal_out_of_scope")
    chosen = next((d for d in in_scope if _session_matches(d, session_id, session_role)), None)
    if chosen is None:
        return _denied("not_delegated_session")

    if lock_delegation:
        chosen = await conn.fetchrow(
            "SELECT * "
            "FROM project_document_approval_delegations "
            "WHERE id=$1 AND tenant_id=$2::uuid AND project_key=$3 FOR UPDATE",
            chosen["id"], tenant, project)
        if (not chosen or chosen["revoked_at"] is not None
                or chosen["expires_at"] <= datetime.now(timezone.utc)):
            return _denied("delegation_missing")
        if chosen["goal_id"] is not None and chosen["goal_id"] not in goal_ids:
            return _denied("goal_out_of_scope")
        if not _session_matches(chosen, session_id, session_role):
            return _denied("not_delegated_session")

    rows = await conn.fetch(
        "SELECT * FROM project_document_reviews WHERE revision_id=$1 ORDER BY id", revision_id)
    problem = cross_review_problem(rows)
    if problem:
        return _denied(problem)
    return {"ok": True, "delegation": {"delegation_id": str(chosen["id"]), "granted_by": chosen["granted_by"],
                                       "grantee": chosen["grantee"]}}
