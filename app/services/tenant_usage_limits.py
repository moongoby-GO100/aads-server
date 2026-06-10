from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

import asyncpg
import structlog

from app.core.db_pool import get_pool

logger = structlog.get_logger(__name__)

_soft_bypass_usage_limits: ContextVar[bool] = ContextVar(
    "soft_bypass_usage_limits",
    default=False,
)


class TenantUsageLimitExceeded(Exception):
    """Raised when a tenant has crossed a hard usage limit."""

    def __init__(self, decision: "UsageLimitDecision") -> None:
        self.decision = decision
        super().__init__(decision.message)


@dataclass(frozen=True)
class TenantMonthlyUsage:
    tenant_id: str
    month_start: datetime
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class TenantPlanPolicy:
    plan_key: str
    monthly_token_limit: int
    monthly_cost_limit_usd: Decimal
    monthly_call_limit: int
    soft_limit_ratio: Decimal
    hard_limit_ratio: Decimal
    internal_exempt: bool = False


@dataclass(frozen=True)
class UsageLimitDecision:
    allowed: bool
    status: str
    tenant_id: str
    operation: str
    usage: TenantMonthlyUsage
    policy: TenantPlanPolicy
    message: str
    limit_name: Optional[str] = None
    admin_override: bool = False
    internal_exempt: bool = False


def set_soft_bypass_usage_limits(enabled: bool = True):
    """Return a ContextVar token for request-scoped soft-only limit handling."""
    return _soft_bypass_usage_limits.set(bool(enabled))


def reset_soft_bypass_usage_limits(token) -> None:
    _soft_bypass_usage_limits.reset(token)


DEFAULT_PLAN_POLICIES: dict[str, TenantPlanPolicy] = {
    "free": TenantPlanPolicy("free", 1_000_000, Decimal("20"), 1_000, Decimal("0.8"), Decimal("1.0")),
    "team": TenantPlanPolicy("team", 10_000_000, Decimal("200"), 10_000, Decimal("0.8"), Decimal("1.0")),
    "enterprise": TenantPlanPolicy("enterprise", 100_000_000, Decimal("2000"), 100_000, Decimal("0.8"), Decimal("1.0")),
    "internal": TenantPlanPolicy("internal", 0, Decimal("0"), 0, Decimal("0.9"), Decimal("1.0"), internal_exempt=True),
}


def current_month_start(now: Optional[datetime] = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _tenant_plan_key(tenant: Mapping[str, Any]) -> str:
    metadata = tenant.get("metadata") or {}
    if isinstance(metadata, str):
        return "free"
    plan = metadata.get("plan") or metadata.get("plan_key") or tenant.get("plan_key")
    if plan:
        return str(plan).strip().lower()
    if str(tenant.get("kind") or "").lower() == "internal" or str(tenant.get("slug") or "").lower() == "internal":
        return "internal"
    return "free"


def _limit_ratio(used: Decimal, limit: Decimal) -> Decimal:
    if limit <= 0:
        return Decimal("0")
    return used / limit


def evaluate_usage_limit(
    *,
    tenant_id: str,
    operation: str,
    usage: TenantMonthlyUsage,
    policy: TenantPlanPolicy,
    projected_tokens: int = 0,
    projected_cost_usd: Decimal | float | str = Decimal("0"),
    projected_calls: int = 1,
    admin_override: bool = False,
) -> UsageLimitDecision:
    if policy.internal_exempt:
        return UsageLimitDecision(
            allowed=True,
            status="internal_exempt",
            tenant_id=tenant_id,
            operation=operation,
            usage=usage,
            policy=policy,
            message="internal tenant usage is exempt",
            internal_exempt=True,
        )
    if admin_override:
        return UsageLimitDecision(
            allowed=True,
            status="admin_override",
            tenant_id=tenant_id,
            operation=operation,
            usage=usage,
            policy=policy,
            message="admin override allows this usage",
            admin_override=True,
        )

    projected_cost = _decimal(projected_cost_usd)
    checks = (
        ("tokens", Decimal(usage.total_tokens + max(0, int(projected_tokens))), Decimal(policy.monthly_token_limit)),
        ("cost", usage.cost_usd + projected_cost, policy.monthly_cost_limit_usd),
        ("calls", Decimal(usage.calls + max(0, int(projected_calls))), Decimal(policy.monthly_call_limit)),
    )

    soft_limit: Optional[str] = None
    for limit_name, used, limit in checks:
        if limit <= 0:
            continue
        ratio = _limit_ratio(used, limit)
        if ratio >= policy.hard_limit_ratio:
            return UsageLimitDecision(
                allowed=False,
                status="hard_limit",
                tenant_id=tenant_id,
                operation=operation,
                usage=usage,
                policy=policy,
                message=f"tenant monthly {limit_name} limit exceeded",
                limit_name=limit_name,
            )
        if ratio >= policy.soft_limit_ratio and soft_limit is None:
            soft_limit = limit_name

    if soft_limit:
        return UsageLimitDecision(
            allowed=True,
            status="soft_limit",
            tenant_id=tenant_id,
            operation=operation,
            usage=usage,
            policy=policy,
            message=f"tenant monthly {soft_limit} soft limit reached",
            limit_name=soft_limit,
        )

    return UsageLimitDecision(
        allowed=True,
        status="ok",
        tenant_id=tenant_id,
        operation=operation,
        usage=usage,
        policy=policy,
        message="tenant usage within monthly limits",
    )


async def _fetch_tenant(conn: asyncpg.Connection, tenant_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT id::text, slug, kind, metadata FROM tenants WHERE id = $1::uuid AND deleted_at IS NULL",
        tenant_id,
    )
    if not row:
        raise TenantUsageLimitExceeded(
            UsageLimitDecision(
                allowed=False,
                status="tenant_not_found",
                tenant_id=tenant_id,
                operation="unknown",
                usage=TenantMonthlyUsage(tenant_id, current_month_start(), 0, 0, 0, 0, Decimal("0")),
                policy=DEFAULT_PLAN_POLICIES["free"],
                message="tenant not found",
            )
        )
    return dict(row)


async def _fetch_policy(conn: asyncpg.Connection, plan_key: str, tenant: Mapping[str, Any]) -> TenantPlanPolicy:
    fallback = DEFAULT_PLAN_POLICIES.get(plan_key) or DEFAULT_PLAN_POLICIES["free"]
    try:
        row = await conn.fetchrow(
            """
            SELECT plan_key, monthly_token_limit, monthly_cost_limit_usd,
                   monthly_call_limit, soft_limit_ratio, hard_limit_ratio
            FROM tenant_plan_limits
            WHERE plan_key = $1 AND is_active = TRUE
            """,
            plan_key,
        )
    except asyncpg.UndefinedTableError:
        row = None
    if not row:
        return fallback
    return TenantPlanPolicy(
        plan_key=str(row["plan_key"]),
        monthly_token_limit=int(row["monthly_token_limit"] or 0),
        monthly_cost_limit_usd=_decimal(row["monthly_cost_limit_usd"]),
        monthly_call_limit=int(row["monthly_call_limit"] or 0),
        soft_limit_ratio=_decimal(row["soft_limit_ratio"]),
        hard_limit_ratio=_decimal(row["hard_limit_ratio"]),
        internal_exempt=str(tenant.get("kind") or "").lower() == "internal" or str(tenant.get("slug") or "").lower() == "internal",
    )


async def get_tenant_monthly_usage(tenant_id: str, *, month_start: Optional[datetime] = None) -> TenantMonthlyUsage:
    month = current_month_start(month_start)
    pool = get_pool()
    async with pool.acquire() as conn:
        return await _get_tenant_monthly_usage(conn, tenant_id, month)


async def _get_tenant_monthly_usage(
    conn: asyncpg.Connection,
    tenant_id: str,
    month_start: datetime,
) -> TenantMonthlyUsage:
    row = await conn.fetchrow(
        """
        WITH usage_rows AS (
            SELECT COUNT(*)::bigint AS calls,
                   COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
                   COALESCE(SUM(input_tokens + output_tokens), 0)::bigint AS total_tokens,
                   COALESCE(SUM(cost_usd), 0)::numeric AS cost_usd
              FROM oauth_usage_log
             WHERE tenant_id = $1::uuid
               AND created_at >= $2
               AND error_code IS NULL
            UNION ALL
            SELECT COUNT(*)::bigint,
                   COALESCE(SUM(input_tokens), 0)::bigint,
                   COALESCE(SUM(output_tokens), 0)::bigint,
                   COALESCE(SUM(input_tokens + output_tokens), 0)::bigint,
                   0::numeric
              FROM bg_llm_usage_log
             WHERE tenant_id = $1::uuid
               AND created_at >= $2
               AND success = TRUE
            UNION ALL
            SELECT COALESCE(SUM(llm_calls), 0)::bigint,
                   COALESCE(SUM(input_tokens), 0)::bigint,
                   COALESCE(SUM(output_tokens), 0)::bigint,
                   COALESCE(SUM(input_tokens + output_tokens), 0)::bigint,
                   COALESCE(SUM(cost_usd), 0)::numeric
              FROM cost_tracking
             WHERE tenant_id = $1::uuid
               AND recorded_at >= $2
        )
        SELECT COALESCE(SUM(calls), 0)::bigint AS calls,
               COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
               COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
               COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens,
               COALESCE(SUM(cost_usd), 0)::numeric AS cost_usd
          FROM usage_rows
        """,
        tenant_id,
        month_start,
    )
    return TenantMonthlyUsage(
        tenant_id=tenant_id,
        month_start=month_start,
        calls=int(row["calls"] or 0),
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        total_tokens=int(row["total_tokens"] or 0),
        cost_usd=_decimal(row["cost_usd"]),
    )


async def tenant_has_admin_override(conn: asyncpg.Connection, tenant_id: str, user_id: Optional[str] = None) -> bool:
    if user_id == "admin":
        return True
    try:
        row = await conn.fetchrow(
            """
            SELECT 1
              FROM tenant_usage_overrides
             WHERE tenant_id = $1::uuid
               AND revoked_at IS NULL
               AND expires_at > NOW()
             LIMIT 1
            """,
            tenant_id,
        )
        return bool(row)
    except asyncpg.UndefinedTableError:
        return False


async def check_tenant_usage_limit(
    tenant_id: str,
    *,
    operation: str,
    projected_tokens: int = 0,
    projected_cost_usd: Decimal | float | str = Decimal("0"),
    projected_calls: int = 1,
    user_id: Optional[str] = None,
    admin_override: bool = False,
    raise_on_block: bool = True,
) -> UsageLimitDecision:
    pool = get_pool()
    month = current_month_start()
    async with pool.acquire() as conn:
        tenant = await _fetch_tenant(conn, tenant_id)
        policy = await _fetch_policy(conn, _tenant_plan_key(tenant), tenant)
        usage = await _get_tenant_monthly_usage(conn, tenant_id, month)
        override = admin_override or await tenant_has_admin_override(conn, tenant_id, user_id=user_id)

    decision = evaluate_usage_limit(
        tenant_id=tenant_id,
        operation=operation,
        usage=usage,
        policy=policy,
        projected_tokens=projected_tokens,
        projected_cost_usd=projected_cost_usd,
        projected_calls=projected_calls,
        admin_override=override,
    )
    if not decision.allowed and _soft_bypass_usage_limits.get():
        logger.warning(
            "tenant_usage_soft_bypass",
            tenant_id=tenant_id,
            operation=operation,
            limit=decision.limit_name,
            plan=policy.plan_key,
        )
        return UsageLimitDecision(
            allowed=True,
            status="soft_bypass",
            tenant_id=tenant_id,
            operation=operation,
            usage=decision.usage,
            policy=decision.policy,
            message="tenant hard limit recorded as soft bypass",
            limit_name=decision.limit_name,
            admin_override=decision.admin_override,
            internal_exempt=decision.internal_exempt,
        )

    if not decision.allowed:
        logger.warning(
            "tenant_usage_hard_limit",
            tenant_id=tenant_id,
            operation=operation,
            limit=decision.limit_name,
            plan=policy.plan_key,
        )
        if raise_on_block:
            raise TenantUsageLimitExceeded(decision)
    elif decision.status == "soft_limit":
        logger.warning(
            "tenant_usage_soft_limit",
            tenant_id=tenant_id,
            operation=operation,
            limit=decision.limit_name,
            plan=policy.plan_key,
        )
    return decision


def _usage_ratio(used: Decimal | int, limit: Decimal | int) -> float:
    limit_decimal = _decimal(limit)
    if limit_decimal <= 0:
        return 0.0
    ratio = _decimal(used) / limit_decimal
    return float(round(ratio, 4))


async def get_tenant_usage_summary(tenant_id: str) -> dict[str, Any]:
    month = current_month_start()
    pool = get_pool()
    async with pool.acquire() as conn:
        tenant = await _fetch_tenant(conn, tenant_id)
        policy = await _fetch_policy(conn, _tenant_plan_key(tenant), tenant)
        usage = await _get_tenant_monthly_usage(conn, tenant_id, month)

    decision = evaluate_usage_limit(
        tenant_id=tenant_id,
        operation="tenant_usage_summary",
        usage=usage,
        policy=policy,
        projected_calls=0,
    )
    return {
        "tenant_id": tenant_id,
        "month_start": usage.month_start.isoformat(),
        "status": decision.status,
        "limit_name": decision.limit_name,
        "allowed": decision.allowed,
        "plan": {
            "plan_key": policy.plan_key,
            "monthly_token_limit": policy.monthly_token_limit,
            "monthly_cost_limit_usd": str(policy.monthly_cost_limit_usd),
            "monthly_call_limit": policy.monthly_call_limit,
            "soft_limit_ratio": str(policy.soft_limit_ratio),
            "hard_limit_ratio": str(policy.hard_limit_ratio),
            "internal_exempt": policy.internal_exempt,
        },
        "usage": {
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": str(usage.cost_usd),
        },
        "ratios": {
            "tokens": _usage_ratio(usage.total_tokens, policy.monthly_token_limit),
            "cost": _usage_ratio(usage.cost_usd, policy.monthly_cost_limit_usd),
            "calls": _usage_ratio(usage.calls, policy.monthly_call_limit),
        },
    }


async def resolve_tenant_id_for_session(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            tenant_id = await conn.fetchval(
                "SELECT tenant_id::text FROM chat_sessions WHERE id = $1::uuid",
                session_id,
            )
        return str(tenant_id) if tenant_id else None
    except Exception as e:
        logger.debug("resolve_tenant_id_for_session_failed", session_id=session_id[:8], error=str(e)[:120])
        return None


async def record_tenant_llm_usage(
    *,
    tenant_id: Optional[str],
    session_id: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal | float | str = Decimal("0"),
    project: str = "chat",
) -> None:
    resolved_tenant_id = tenant_id or await resolve_tenant_id_for_session(session_id)
    if not resolved_tenant_id:
        return
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cost_tracking
                    (tenant_id, task_id, project, model, input_tokens, output_tokens, cost_usd, llm_calls)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, 1)
                """,
                resolved_tenant_id,
                session_id,
                project,
                model,
                int(input_tokens or 0),
                int(output_tokens or 0),
                _decimal(cost_usd),
            )
    except Exception as e:
        logger.debug("record_tenant_llm_usage_failed", session_id=session_id[:8], error=str(e)[:120])
