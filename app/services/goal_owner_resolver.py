"""목표 생성 시 주도담당(owner_role_key, owner_session_id) 확정.

목표 생성 경로(`goal_registration.register_goal`)는 이 모듈이 확정한 값만 INSERT 한다.
확정하지 못하면 `owner_required` 와 후보 목록을 돌려주고 목표 행은 만들지 않는다.

확정 순서 (앞 규칙이 이기고, 뒤로 추측하지 않는다):
  1. 명시값 — owner_session_id 가 오면 그 세션을 검증한다(같은 tenant, 같은 project).
     owner_role_key 만 오면 (tenant, project, role_key) 활성 담당 세션을 찾는다.
  2. 생성 세션 — 생성 주체 세션이 같은 project 워크스페이스에 있으면 그 세션과 role.
  3. 위가 안 되면 후보를 모아 `owner_required`. project+role 을 알 수 없으면 고르지 않는다.

project 경계는 `goal_owner_binding_policy`(M11) 한 곳을 쓴다. CEO 통합지시 세션은
조정자로만 붙을 수 있고 주도 담당이 될 수 없다.

이 모듈은 세션을 **만들지 않는다.** 세션 생성은 승인 카드를 거치는
`owner_session_provision` 만의 일이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

OWNER_REQUIRED = "owner_required"
CANDIDATE_LIMIT = 20
OWNER_ERRORS = frozenset({
    OWNER_REQUIRED,
    "owner_session_not_found",
    "cross_project_owner_denied",
    "owner_role_mismatch",
    "portfolio_coordinator_requires_ceo_workspace",
})


def goal_owner_binding_policy(
    goal_project: str,
    session_project: str,
    collaboration_role: str,
    *,
    as_lead: bool,
) -> tuple[bool, str | None]:
    """Enforce local execution ownership with one explicit CEO coordinator exception."""
    goal_key = (goal_project or "").strip().upper()
    session_key = (session_project or "").strip().upper()
    if goal_key and session_key == goal_key:
        if collaboration_role == "portfolio_coordinator":
            return False, "portfolio_coordinator_requires_ceo_workspace"
        return True, None
    if session_key == "CEO" and collaboration_role == "portfolio_coordinator" and not as_lead:
        return True, None
    return False, "cross_project_owner_denied"


@dataclass(frozen=True)
class OwnerResolution:
    owner_role_key: Optional[str] = None
    owner_session_id: Optional[str] = None
    source: str = ""
    error: Optional[str] = None
    reason: Optional[str] = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.error}
        if self.reason:
            out["reason"] = self.reason
        if self.error == OWNER_REQUIRED:
            out["candidates"] = self.candidates
            out["hint"] = (
                "candidates 중 하나를 골라 owner_role_key(+owner_session_id)로 다시 등록하세요."
            )
        return out


_SESSION_SQL = """
SELECT s.id::text AS session_id, COALESCE(s.role_key, '') AS role_key,
       COALESCE(s.title, '') AS title, COALESCE(w.project_key, '') AS project_key
  FROM chat_sessions s
  JOIN chat_workspaces w ON w.id = s.workspace_id AND w.tenant_id = s.tenant_id
 WHERE s.id = $1::uuid AND s.tenant_id = $2::uuid
"""

_ASSIGNMENT_SQL = """
SELECT a.session_id::text AS session_id, a.role_key AS role_key,
       COALESCE(s.title, '') AS title, COALESCE(w.project_key, '') AS project_key
  FROM project_role_assignments a
  JOIN chat_sessions s ON s.id = a.session_id AND s.tenant_id = a.tenant_id
  JOIN chat_workspaces w ON w.id = s.workspace_id AND w.tenant_id = s.tenant_id
 WHERE a.tenant_id = $1::uuid AND upper(a.project) = upper($2) AND a.active
   AND ($3::text IS NULL OR a.role_key = $3)
 ORDER BY a.role_key, a.session_id
 LIMIT 50
"""

_WORKSPACE_SESSIONS_SQL = """
SELECT s.id::text AS session_id, s.role_key AS role_key,
       COALESCE(s.title, '') AS title, COALESCE(w.project_key, '') AS project_key
  FROM chat_sessions s
  JOIN chat_workspaces w ON w.id = s.workspace_id AND w.tenant_id = s.tenant_id
 WHERE s.tenant_id = $1::uuid AND upper(w.project_key) = upper($2)
   AND COALESCE(s.role_key, '') <> ''
 ORDER BY s.updated_at DESC NULLS LAST
 LIMIT 50
"""


def _lead_allowed(project: str, session: Mapping[str, Any]) -> tuple[bool, str | None]:
    return goal_owner_binding_policy(
        project, str(session.get("project_key") or ""), "project_owner", as_lead=True,
    )


def _candidate(row: Mapping[str, Any], source: str) -> dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "role_key": str(row["role_key"] or ""),
        "title": str(row.get("title") or ""),
        "source": source,
    }


async def _candidates(conn: Any, tenant_id: str, project: str) -> list[dict[str, Any]]:
    """같은 tenant·project 에서 고를 수 있는 담당. 활성 배정이 앞, 역할만 있는 세션이 뒤."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, sql, args in (
        ("assignment", _ASSIGNMENT_SQL, (tenant_id, project, None)),
        ("workspace_session", _WORKSPACE_SESSIONS_SQL, (tenant_id, project)),
    ):
        for row in await conn.fetch(sql, *args):
            if str(row["session_id"]) in seen or not _lead_allowed(project, row)[0]:
                continue
            seen.add(str(row["session_id"]))
            out.append(_candidate(row, source))
            if len(out) >= CANDIDATE_LIMIT:
                return out
    return out


async def _required(
    conn: Any, tenant_id: str, project: str, reason: str
) -> OwnerResolution:
    return OwnerResolution(
        error=OWNER_REQUIRED, reason=reason, candidates=await _candidates(conn, tenant_id, project),
    )


async def _assignment_role_of_session(
    conn: Any, tenant_id: str, project: str, session_id: str
) -> str:
    for row in await conn.fetch(_ASSIGNMENT_SQL, tenant_id, project, None):
        if str(row["session_id"]) == session_id:
            return str(row["role_key"] or "")
    return ""


async def resolve_goal_owner(
    conn: Any,
    *,
    tenant_id: str,
    project: str,
    creator_session_id: Optional[str] = None,
    owner_role_key: Optional[str] = None,
    owner_session_id: Optional[str] = None,
) -> OwnerResolution:
    project = (project or "").strip().upper()
    role = (owner_role_key or "").strip()

    if owner_session_id:
        sess = await conn.fetchrow(_SESSION_SQL, owner_session_id, tenant_id)
        if not sess:
            return OwnerResolution(error="owner_session_not_found")
        allowed, reason = _lead_allowed(project, sess)
        if not allowed:
            return OwnerResolution(error=reason)
        session_role = str(sess["role_key"] or "")
        if role and session_role and role != session_role:
            return OwnerResolution(error="owner_role_mismatch")
        effective = (
            session_role or role
            or await _assignment_role_of_session(conn, tenant_id, project, str(sess["session_id"]))
        )
        if not effective:
            return await _required(conn, tenant_id, project, "owner_session_has_no_role")
        return OwnerResolution(
            owner_role_key=effective, owner_session_id=str(sess["session_id"]), source="explicit_session",
        )

    if role:
        if creator_session_id:
            creator = await conn.fetchrow(_SESSION_SQL, creator_session_id, tenant_id)
            if (
                creator
                and str(creator["role_key"] or "") == role
                and _lead_allowed(project, creator)[0]
            ):
                return OwnerResolution(
                    owner_role_key=role, owner_session_id=str(creator["session_id"]),
                    source="creator_session",
                )
        rows = [
            r for r in await conn.fetch(_ASSIGNMENT_SQL, tenant_id, project, role)
            if _lead_allowed(project, r)[0]
        ]
        if len(rows) == 1:
            return OwnerResolution(
                owner_role_key=role, owner_session_id=str(rows[0]["session_id"]),
                source="role_assignment",
            )
        return await _required(
            conn, tenant_id, project,
            "no_active_assignment_for_role" if not rows else "ambiguous_role_assignment",
        )

    if creator_session_id:
        creator = await conn.fetchrow(_SESSION_SQL, creator_session_id, tenant_id)
        if creator and _lead_allowed(project, creator)[0]:
            effective = str(creator["role_key"] or "") or await _assignment_role_of_session(
                conn, tenant_id, project, str(creator["session_id"]),
            )
            if effective:
                return OwnerResolution(
                    owner_role_key=effective, owner_session_id=str(creator["session_id"]),
                    source="creator_session",
                )
            return await _required(conn, tenant_id, project, "creator_session_has_no_role")
        return await _required(conn, tenant_id, project, "creator_not_in_project")
    return await _required(conn, tenant_id, project, "no_owner_hint")


_MISSING_OWNER_SQL = """
SELECT id::text AS goal_id, tenant_id::text AS tenant_id, project, status, title,
       COALESCE(owner_role_key, '') AS owner_role_key,
       owner_session_id::text AS owner_session_id
  FROM goals
 WHERE status = ANY($1::text[])
   AND (COALESCE(owner_role_key, '') = '' OR owner_session_id IS NULL)
   AND ($2::uuid IS NULL OR tenant_id = $2::uuid)
 ORDER BY project, created_at
"""


async def find_goals_missing_owner(
    conn: Any,
    *,
    statuses: tuple[str, ...] = ("active", "draft"),
    tenant_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(_MISSING_OWNER_SQL, list(statuses), tenant_id)
    return [dict(r) for r in rows]


async def plan_owner_fix(conn: Any, goal: Mapping[str, Any]) -> dict[str, Any]:
    """누락 목표 한 건의 보정안. 후보가 하나로 확정될 때만 `fix` 를 채운다.

    이미 있는 칸은 덮어쓰지 않는다. 아무 칸도 없을 때만, 후보 전체가 활성 배정 1개뿐인
    project 를 확정으로 본다 — 다른 후보가 하나라도 있으면 모호 건으로 보고만 한다.
    """
    res = await resolve_goal_owner(
        conn,
        tenant_id=str(goal["tenant_id"]),
        project=str(goal["project"]),
        owner_role_key=str(goal.get("owner_role_key") or "") or None,
        owner_session_id=str(goal.get("owner_session_id") or "") or None,
    )
    plan: dict[str, Any] = {
        "goal_id": goal["goal_id"], "tenant_id": goal["tenant_id"], "project": goal["project"],
        "status": goal["status"], "title": goal.get("title"), "fix": None,
    }
    if res.ok:
        plan.update(fix={"owner_role_key": res.owner_role_key, "owner_session_id": res.owner_session_id},
                    reason=res.source)
        return plan
    plan["reason"] = res.reason or res.error
    plan["candidates"] = res.candidates
    has_any = bool(goal.get("owner_role_key")) or bool(goal.get("owner_session_id"))
    only = res.candidates[0] if len(res.candidates) == 1 else None
    if res.error == OWNER_REQUIRED and not has_any and only and only["source"] == "assignment":
        plan.update(
            fix={"owner_role_key": only["role_key"], "owner_session_id": only["session_id"]},
            reason="single_active_assignment",
        )
    return plan
