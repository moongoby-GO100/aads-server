"""M14 work-item review, approval, and capability-grant API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.core.goal_work_hierarchy_policy import (
    goal_work_hierarchy_enabled,
    workflow_approval_enabled,
)
from app.services.goal_work_hierarchy import resolve_actor_scope
from app.services.goal_workflow_approval import (
    approval_preview,
    create_change_set,
    create_grant,
    decide_change_set,
    execute_change_set,
    preview_grant,
    reconcile_grant_use,
    request_outbox_retry,
    reserve_grant_use,
    review_item,
    revoke_grant,
    route_change_set,
    submit_review,
)

router = APIRouter()
member = require_tenant_role(TenantRole.MEMBER)
viewer = require_tenant_role(TenantRole.VIEWER)
member_dependency = Depends(member)
viewer_dependency = Depends(viewer)


class ChangeSetRequest(BaseModel):
    base_version: int = Field(ge=1)
    patch: list[dict[str, Any]]
    action: Literal["create", "update", "assign", "cancel", "execute", "accept"] = "update"
    rationale: str = Field(min_length=1)
    expected_effect: str = Field(min_length=1)
    rollback_plan: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    environment: Literal["dev", "staging", "production"] = "dev"
    risk_factors: list[str] = Field(default_factory=list)


class ExecuteRequest(BaseModel):
    execution_key: str = Field(min_length=1, max_length=300)
    owner_instance: str = Field(min_length=1)
    owner_epoch: int = Field(ge=1)


class ReviewRequest(BaseModel):
    reason: str = ""


class DecisionRequest(BaseModel):
    approve: bool
    reason: str = ""
    bulk: bool = False


class GrantRequest(BaseModel):
    principal_session_id: UUID
    assignment_id: UUID
    # Identity is derived from the authenticated actor and the approved server
    # record. Legacy fields remain parseable for compatibility but are ignored.
    approval_request_id: UUID
    approved_by: UUID | None = None
    requested_by: UUID | None = None
    milestone_id: UUID | None = None
    epic_id: UUID | None = None
    story_id: UUID | None = None
    actions: list[str] = Field(min_length=1)
    tool_groups: list[str] = Field(default_factory=list)
    max_risk_tier: Literal["A0", "A1", "A2"] = "A1"
    environments: list[Literal["dev", "staging", "production"]] = Field(default_factory=lambda: ["dev"])
    conditions: dict[str, Any] = Field(default_factory=dict)
    max_executions: int = Field(default=1, ge=1)
    max_files: int = Field(default=0, ge=0)
    max_rows: int = Field(default=0, ge=0)
    max_cost_usd: float = Field(default=0, ge=0)
    max_parallel: int = Field(default=1, ge=1)
    max_duration_seconds: int = Field(default=0, ge=0)
    valid_from: datetime
    expires_at: datetime
    idle_timeout_seconds: int | None = Field(default=None, ge=1)
    delegation_depth: Literal[0, 1] = 0
    parent_grant_id: UUID | None = None
    revocation_strategy: Literal["cancel_now", "finish_current", "compensate"] = "cancel_now"


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1)


class RetryRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class GrantUseResultRequest(BaseModel):
    outcome: Literal["completed", "failed", "unknown"]
    actual_budget: dict[str, int | float] = Field(default_factory=dict)


class SimulationRequest(BaseModel):
    input: dict[str, Any]


class PolicyReplayRequest(BaseModel):
    candidate_policy_id: UUID
    decision_ids: list[UUID] = Field(min_length=1, max_length=500)


class PolicyPromotionRequest(BaseModel):
    target_mode: Literal["canary", "enabled", "rollback"]
    reason: str = Field(min_length=1)


class PreconditionRequest(BaseModel):
    expected_parent_version: int | None = Field(default=None, ge=0)
    assignment_id: UUID | None = None


class PolicyInputRequest(PreconditionRequest):
    action: Literal["create", "update", "assign", "cancel", "execute", "accept"]
    base_version: int = Field(ge=1)
    patch: list[dict[str, Any]] = Field(default_factory=list)
    environment: Literal["dev", "staging", "production"] = "dev"
    risk_factors: list[str] = Field(default_factory=list)


def _identity(context: dict[str, Any]) -> tuple[str, str, bool]:
    if not isinstance(context, dict):
        raise HTTPException(401, detail={"code": "unauthenticated"})
    user = context.get("user", {})
    tenant = str(context.get("tenant", {}).get("id") or "").strip()
    user_id = str(user.get("user_id") or "").strip()
    if not tenant or not user_id:
        raise HTTPException(401, detail={"code": "unauthenticated"})
    return tenant, user_id, bool(user.get("is_internal_admin"))


def _require_m14() -> None:
    if not goal_work_hierarchy_enabled() or not workflow_approval_enabled():
        raise HTTPException(404, detail={"code": "goal_workflow_approval_disabled"})


def _require_w13() -> None:
    if not goal_work_hierarchy_enabled():
        raise HTTPException(404, detail={"code": "goal_work_hierarchy_disabled"})


async def _actor(conn: Any, context: dict[str, Any], session_id: str | None):
    tenant, user, admin = _identity(context)
    return await resolve_actor_scope(conn, tenant_id=tenant, user_id=user,
                                     actor_session_id=session_id, internal_admin=admin)


@router.post("/work-items/{item_id}/preconditions/preview")
async def post_precondition_preview(
    item_id: str, req: PreconditionRequest, context=viewer_dependency,
    session_id: str | None = Header(None, alias="X-Chat-Session-ID"),
):
    """Persist a server-computed snapshot; request bodies cannot supply its fields."""
    _require_w13()
    from app.core.db_pool import get_pool
    from app.services.goal_policy_preconditions import compute_preconditions

    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant_id',$1,true)", tenant)
        actor = await _actor(conn, context, session_id)
        return await compute_preconditions(
            conn, tenant_id=tenant, item_id=item_id, actor=actor,
            assignment_id=str(req.assignment_id) if req.assignment_id else None,
            expected_parent_version=req.expected_parent_version,
        )


@router.post("/work-items/{item_id}/policy-inputs", status_code=201)
async def post_policy_input(
    item_id: str, req: PolicyInputRequest, context=member_dependency,
    session_id: str | None = Header(None, alias="X-Chat-Session-ID"),
):
    """Create evaluator input only. W-13 never returns or implies AUTO."""
    _require_w13()
    from app.core.db_pool import get_pool
    from app.services.goal_policy_preconditions import create_policy_input

    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant_id',$1,true)", tenant)
        actor = await _actor(conn, context, session_id)
        return await create_policy_input(
            conn, tenant_id=tenant, item_id=item_id, actor=actor,
            payload=req.model_dump(mode="json"),
        )


@router.post("/work-items/{item_id}/change-sets", status_code=201)
async def post_change_set(item_id: str, req: ChangeSetRequest, context=member_dependency,
                          session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        actor = await _actor(conn, context, session_id)
        result = await create_change_set(
            conn, tenant_id=tenant, actor=actor, target_id=item_id,
            payload=req.model_dump(mode="json"),
        )
        routing = await route_change_set(
            conn, tenant_id=tenant, change_set_id=result["id"], actor=actor,
        )
    return {"change_set": result, **routing}


@router.post("/work-item-change-sets/{change_set_id}/execute")
async def post_execute(change_set_id: str, req: ExecuteRequest, context=member_dependency,
                       session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        actor = await _actor(conn, context, session_id)
        return await execute_change_set(conn, tenant_id=tenant, change_set_id=change_set_id,
                                        actor=actor, **req.model_dump())


@router.post("/work-item-change-sets/{change_set_id}/decision")
async def post_decision(change_set_id: str, req: DecisionRequest, context=member_dependency,
                        session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await decide_change_set(conn, tenant_id=tenant, change_set_id=change_set_id,
                                       actor=await _actor(conn, context, session_id), **req.model_dump())


@router.get("/work-items/{item_id}/approval-preview")
async def get_approval_preview(item_id: str, context=viewer_dependency,
                               session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn:
        return await approval_preview(conn, tenant_id=tenant, item_id=item_id,
                                      actor=await _actor(conn, context, session_id))


@router.post("/work-items/{item_id}/retry-delivery")
async def post_retry_delivery(
    item_id: str, req: RetryRequest, context=member_dependency,
    session_id: str | None = Header(None, alias="X-Chat-Session-ID"),
):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await request_outbox_retry(
            conn, tenant_id=tenant, item_id=item_id,
            actor=await _actor(conn, context, session_id), reason=req.reason,
        )


@router.post("/work-items/{item_id}/submit-review")
async def post_submit_review(item_id: str, context=member_dependency,
                             session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await submit_review(conn, tenant_id=tenant, item_id=item_id,
                                   actor=await _actor(conn, context, session_id))


async def _review(item_id: str, req: ReviewRequest, accept: bool, context: dict[str, Any], session_id: str | None):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await review_item(conn, tenant_id=tenant, item_id=item_id,
                                 actor=await _actor(conn, context, session_id), accept=accept, reason=req.reason)


@router.post("/work-items/{item_id}/accept")
async def post_accept(item_id: str, req: ReviewRequest, context=member_dependency,
                      session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    return await _review(item_id, req, True, context, session_id)


@router.post("/work-items/{item_id}/request-changes")
async def post_request_changes(item_id: str, req: ReviewRequest, context=member_dependency,
                               session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    return await _review(item_id, req, False, context, session_id)


@router.post("/goals/{goal_id}/auto-approval-grants/preview")
async def post_grant_preview(goal_id: str, req: GrantRequest, context=member_dependency,
                             session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    if not session_id:
        raise HTTPException(403, detail={"code": "project_scope_denied"})
    return preview_grant(req.model_dump(mode="json"), actor_session_id=session_id)


@router.post("/goals/{goal_id}/auto-approval-grants", status_code=201)
async def post_grant(goal_id: str, req: GrantRequest, context=member_dependency,
                     session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await create_grant(conn, tenant_id=tenant, goal_id=goal_id,
                                  actor=await _actor(conn, context, session_id),
                                  payload=req.model_dump(mode="json"))


@router.get("/goals/{goal_id}/auto-approval-grants")
async def get_grants(goal_id: str, context=viewer_dependency,
                     session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn:
        actor = await _actor(conn, context, session_id)
        goal = await conn.fetchrow("SELECT project FROM goals WHERE id=$1::uuid AND tenant_id=$2::uuid",
                                   goal_id, tenant)
        if not goal or not actor.may_access(str(goal["project"])):
            raise HTTPException(404, detail={"code": "goal_not_found"})
        rows = await conn.fetch(
            """SELECT *,max_executions-used_executions AS remaining_uses FROM goal_auto_approval_grants
               WHERE tenant_id=$1::uuid AND goal_id=$2::uuid AND project=$3 ORDER BY issued_at DESC""",
            tenant, goal_id, goal["project"])
        return [dict(row) for row in rows]


@router.post("/auto-approval-grants/{grant_id}/revoke")
async def post_revoke(grant_id: str, req: RevokeRequest, context=member_dependency,
                      session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await revoke_grant(conn, tenant_id=tenant, grant_id=grant_id,
                                  actor=await _actor(conn, context, session_id), reason=req.reason)


@router.get("/auto-approval-grants/{grant_id}/usage")
async def get_usage(grant_id: str, context=viewer_dependency,
                    session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn:
        actor = await _actor(conn, context, session_id)
        grant = await conn.fetchrow(
            "SELECT goal_id::text,project FROM goal_auto_approval_grants WHERE id=$1::uuid AND tenant_id=$2::uuid",
            grant_id, tenant)
        if not grant or not actor.may_access(str(grant["project"])):
            raise HTTPException(404, detail={"code": "grant_not_found"})
        rows = await conn.fetch(
            """SELECT u.* FROM goal_auto_approval_uses u
               JOIN goal_auto_approval_grants g ON g.id=u.grant_id AND g.tenant_id=u.tenant_id
               WHERE u.tenant_id=$1::uuid AND u.grant_id=$2::uuid AND g.goal_id=$3::uuid
               ORDER BY u.reserved_at""", tenant, grant_id, grant["goal_id"])
        return [dict(row) for row in rows]


@router.post("/auto-approval-grant-uses/{execution_key}/reconcile")
async def post_reconcile_grant_use(execution_key: str, req: GrantUseResultRequest,
                                   context=member_dependency,
                                   session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await reconcile_grant_use(
            conn, tenant_id=tenant, execution_key=execution_key,
            actual_budget=req.actual_budget, outcome=req.outcome,
            actor=await _actor(conn, context, session_id),
        )


@router.post("/goal-policy/simulate")
async def post_simulate(req: SimulationRequest, context=member_dependency,
                        session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await reserve_grant_use(conn, tenant_id=tenant,
                                       actor=await _actor(conn, context, session_id),
                                       request=req.input, simulate=True)


@router.post("/goal-policy/shadow-replay")
async def post_shadow_replay(req: PolicyReplayRequest, context=member_dependency,
                             session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    from app.services.goal_policy_rollout import simulate_policy

    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await simulate_policy(
            conn, tenant_id=tenant, actor=await _actor(conn, context, session_id),
            candidate_policy_id=str(req.candidate_policy_id),
            decision_ids=[str(value) for value in req.decision_ids],
        )


@router.post("/goal-policy/versions/{policy_id}/promote")
async def post_promote_policy(policy_id: str, req: PolicyPromotionRequest,
                              context=member_dependency,
                              session_id: str | None = Header(None, alias="X-Chat-Session-ID")):
    _require_m14()
    from app.core.db_pool import get_pool
    from app.services.goal_policy_rollout import promote_policy

    tenant, _, _ = _identity(context)
    async with get_pool().acquire() as conn, conn.transaction():
        return await promote_policy(
            conn, tenant_id=tenant, actor=await _actor(conn, context, session_id),
            policy_id=policy_id, target_mode=req.target_mode, reason=req.reason,
        )
