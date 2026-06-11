"""Admin user signup and usage dashboard API."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query

from app.auth import require_internal_admin

router = APIRouter(dependencies=[Depends(require_internal_admin)])


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _table_exists(conn, table_name: str) -> bool:
    return bool(
        await conn.fetchval(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = $1
            LIMIT 1
            """,
            table_name,
        )
    )


async def _columns(conn, table_name: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = $1
        """,
        table_name,
    )
    return {str(row["column_name"]) for row in rows}


async def _usage_totals(conn, table_name: str, days: int) -> dict[str, Any]:
    if not await _table_exists(conn, table_name):
        return {"calls": 0, "tokens": 0, "cost_usd": 0.0}

    columns = await _columns(conn, table_name)
    if "created_at" not in columns:
        return {"calls": 0, "tokens": 0, "cost_usd": 0.0}

    input_expr = "COALESCE(input_tokens, 0)" if "input_tokens" in columns else "0"
    output_expr = "COALESCE(output_tokens, 0)" if "output_tokens" in columns else "0"
    cost_expr = "COALESCE(cost_usd, 0)" if "cost_usd" in columns else "0"
    success_filter = ""
    if table_name == "oauth_usage_log" and "error_code" in columns:
        success_filter = "AND error_code IS NULL"
    elif table_name == "bg_llm_usage_log" and "success" in columns:
        success_filter = "AND success = TRUE"

    row = await conn.fetchrow(
        f"""
        SELECT
            COUNT(*)::bigint AS calls,
            COALESCE(SUM(({input_expr}) + ({output_expr})), 0)::bigint AS tokens,
            COALESCE(SUM({cost_expr}), 0)::numeric AS cost_usd
        FROM {table_name}
        WHERE created_at >= NOW() - ($1::int * INTERVAL '1 day')
          {success_filter}
        """,
        days,
    )
    return {
        "calls": _int(row["calls"] if row else 0),
        "tokens": _int(row["tokens"] if row else 0),
        "cost_usd": round(_float(row["cost_usd"] if row else 0), 6),
    }


async def _chat_totals(conn, days: int) -> dict[str, Any]:
    if not (await _table_exists(conn, "chat_sessions") and await _table_exists(conn, "chat_messages")):
        return {"sessions": 0, "messages": 0, "tokens": 0, "cost_usd": 0.0}

    message_columns = await _columns(conn, "chat_messages")
    token_expr = (
        "COALESCE(SUM(COALESCE(m.tokens_in, 0) + COALESCE(m.tokens_out, 0)), 0)::bigint"
        if {"tokens_in", "tokens_out"}.issubset(message_columns)
        else "0::bigint"
    )
    cost_expr = (
        "COALESCE(SUM(COALESCE(m.cost, 0)), 0)::numeric"
        if "cost" in message_columns
        else "0::numeric"
    )
    row = await conn.fetchrow(
        f"""
        SELECT
            COUNT(DISTINCT s.id)::bigint AS sessions,
            COUNT(m.id)::bigint AS messages,
            {token_expr} AS tokens,
            {cost_expr} AS cost_usd
        FROM chat_sessions s
        LEFT JOIN chat_messages m
          ON m.session_id = s.id
         AND m.created_at >= NOW() - ($1::int * INTERVAL '1 day')
        WHERE COALESCE(s.updated_at, s.created_at) >= NOW() - ($1::int * INTERVAL '1 day')
        """,
        days,
    )
    return {
        "sessions": _int(row["sessions"] if row else 0),
        "messages": _int(row["messages"] if row else 0),
        "tokens": _int(row["tokens"] if row else 0),
        "cost_usd": round(_float(row["cost_usd"] if row else 0), 6),
    }


async def _user_rows(conn, days: int, limit: int) -> list[dict[str, Any]]:
    required = (
        await _table_exists(conn, "saas_users")
        and await _table_exists(conn, "tenant_memberships")
        and await _table_exists(conn, "tenants")
    )
    if not required:
        return []

    user_columns = await _columns(conn, "saas_users")
    deleted_filter = "u.deleted_at IS NULL" if "deleted_at" in user_columns else "TRUE"
    status_expr = (
        "COALESCE(u.status, CASE WHEN COALESCE(u.is_active, TRUE) THEN 'active' ELSE 'suspended' END)"
        if "status" in user_columns or "is_active" in user_columns
        else "'active'"
    )
    plan_expr = "COALESCE(u.plan, '')" if "plan" in user_columns else "''"

    session_usage_cte = "SELECT NULL::text AS user_id, NULL::uuid AS tenant_id, 0::bigint AS sessions, 0::bigint AS messages, 0::bigint AS tokens, 0::numeric AS cost_usd, NULL::timestamptz AS last_seen_at WHERE FALSE"
    if await _table_exists(conn, "chat_sessions") and await _table_exists(conn, "chat_messages"):
        session_columns = await _columns(conn, "chat_sessions")
        message_columns = await _columns(conn, "chat_messages")
        user_id_expr = "s.user_id" if "user_id" in session_columns else "NULL::text"
        tenant_id_expr = "s.tenant_id" if "tenant_id" in session_columns else "NULL::uuid"
        token_expr = (
            "COALESCE(SUM(COALESCE(m.tokens_in, 0) + COALESCE(m.tokens_out, 0)), 0)::bigint"
            if {"tokens_in", "tokens_out"}.issubset(message_columns)
            else "0::bigint"
        )
        cost_expr = "COALESCE(SUM(COALESCE(m.cost, 0)), 0)::numeric" if "cost" in message_columns else "0::numeric"
        role_filter = "AND m.role = 'user'" if "role" in message_columns else ""
        session_usage_cte = f"""
            SELECT
                {user_id_expr}::text AS user_id,
                {tenant_id_expr} AS tenant_id,
                COUNT(DISTINCT s.id)::bigint AS sessions,
                COUNT(m.id)::bigint AS messages,
                {token_expr} AS tokens,
                {cost_expr} AS cost_usd,
                MAX(COALESCE(m.created_at, s.updated_at, s.created_at)) AS last_seen_at
            FROM chat_sessions s
            LEFT JOIN chat_messages m
              ON m.session_id = s.id
             AND m.created_at >= NOW() - ($1::int * INTERVAL '1 day')
             {role_filter}
            WHERE COALESCE(s.updated_at, s.created_at) >= NOW() - ($1::int * INTERVAL '1 day')
            GROUP BY 1, 2
        """

    rows = await conn.fetch(
        f"""
        WITH session_usage AS ({session_usage_cte})
        SELECT
            u.id::text AS user_id,
            u.email,
            u.name,
            COALESCE(u.role, 'user') AS role,
            {status_expr} AS status,
            {plan_expr} AS plan,
            dt.name AS default_tenant_name,
            u.created_at,
            u.updated_at,
            COUNT(DISTINCT tm.tenant_id)::int AS tenant_count,
            COALESCE(SUM(su.sessions), 0)::bigint AS sessions_30d,
            COALESCE(SUM(su.messages), 0)::bigint AS messages_30d,
            COALESCE(SUM(su.tokens), 0)::bigint AS tokens_30d,
            COALESCE(SUM(su.cost_usd), 0)::numeric AS cost_30d,
            MAX(su.last_seen_at) AS last_seen_at
        FROM saas_users u
        LEFT JOIN tenants dt ON dt.id = u.default_tenant_id
        LEFT JOIN tenant_memberships tm
          ON tm.user_id = u.id
         AND tm.deleted_at IS NULL
         AND tm.status = 'active'
        LEFT JOIN session_usage su
          ON su.user_id = u.id
          OR su.tenant_id = tm.tenant_id
        WHERE {deleted_filter}
        GROUP BY u.id, u.email, u.name, u.role, {status_expr}, {plan_expr}, dt.name, u.created_at, u.updated_at
        ORDER BY MAX(su.last_seen_at) DESC NULLS LAST, u.created_at DESC NULLS LAST
        LIMIT $2
        """,
        days,
        limit,
    )

    return [
        {
            "user_id": row["user_id"],
            "email": row["email"] or "",
            "name": row["name"] or "",
            "role": row["role"] or "user",
            "status": row["status"] or "active",
            "plan": row["plan"] or "",
            "default_tenant_name": row["default_tenant_name"] or "",
            "tenant_count": _int(row["tenant_count"]),
            "sessions_30d": _int(row["sessions_30d"]),
            "messages_30d": _int(row["messages_30d"]),
            "tokens_30d": _int(row["tokens_30d"]),
            "cost_30d": round(_float(row["cost_30d"]), 6),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "last_seen_at": _iso(row["last_seen_at"]),
        }
        for row in rows
    ]


@router.get("/admin/users/overview")
async def get_admin_users_overview(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(80, ge=1, le=300),
) -> dict[str, Any]:
    """Return signup, tenant, membership, and usage metrics for admin users page."""
    from app.core.db_pool import get_pool

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    pool = get_pool()
    async with pool.acquire() as conn:
        tables = {
            name: await _table_exists(conn, name)
            for name in (
                "saas_users",
                "tenants",
                "tenant_memberships",
                "tenant_invites",
                "chat_sessions",
                "chat_messages",
                "oauth_usage_log",
                "bg_llm_usage_log",
            )
        }

        if not tables["saas_users"]:
            return {
                "generated_at": now.isoformat(),
                "window_days": days,
                "summary": {"total_users": 0, "active_users": 0, "new_users_7d": 0, "new_users_30d": 0},
                "tables": tables,
                "plans": [],
                "membership_roles": [],
                "tenants": [],
                "users": [],
                "daily": [],
            }

        user_columns = await _columns(conn, "saas_users")
        status_expr = (
            "COALESCE(status, CASE WHEN COALESCE(is_active, TRUE) THEN 'active' ELSE 'suspended' END)"
            if "status" in user_columns or "is_active" in user_columns
            else "'active'"
        )
        deleted_expr = "deleted_at IS NOT NULL" if "deleted_at" in user_columns else "FALSE"
        not_deleted_expr = "deleted_at IS NULL" if "deleted_at" in user_columns else "TRUE"
        plan_expr = "COALESCE(NULLIF(plan, ''), 'unassigned')" if "plan" in user_columns else "'unassigned'"

        user_summary = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*)::int AS total_users,
                COUNT(*) FILTER (WHERE {status_expr} = 'active' AND {not_deleted_expr})::int AS active_users,
                COUNT(*) FILTER (WHERE {status_expr} = 'suspended' AND {not_deleted_expr})::int AS suspended_users,
                COUNT(*) FILTER (WHERE {deleted_expr} OR {status_expr} = 'deleted')::int AS deleted_users,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')::int AS new_users_7d,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days')::int AS new_users_30d
            FROM saas_users
            """
        )

        tenant_summary = {
            "total_tenants": 0,
            "customer_tenants": 0,
            "internal_tenants": 0,
            "active_tenants": 0,
        }
        if tables["tenants"]:
            tenant_summary = dict(await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS total_tenants,
                    COUNT(*) FILTER (WHERE kind = 'customer' AND deleted_at IS NULL)::int AS customer_tenants,
                    COUNT(*) FILTER (WHERE kind = 'internal' AND deleted_at IS NULL)::int AS internal_tenants,
                    COUNT(*) FILTER (WHERE status = 'active' AND deleted_at IS NULL)::int AS active_tenants
                FROM tenants
                """
            ))

        membership_summary = {"active_memberships": 0, "internal_active_memberships": 0}
        membership_roles: list[dict[str, Any]] = []
        if tables["tenant_memberships"]:
            membership_summary = dict(await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE tm.status = 'active' AND tm.deleted_at IS NULL)::int AS active_memberships,
                    COUNT(*) FILTER (WHERE t.kind = 'internal' AND tm.status = 'active' AND tm.deleted_at IS NULL)::int AS internal_active_memberships
                FROM tenant_memberships tm
                LEFT JOIN tenants t ON t.id = tm.tenant_id
                """
            ))
            role_rows = await conn.fetch(
                """
                SELECT role, status, COUNT(*)::int AS memberships
                FROM tenant_memberships
                WHERE deleted_at IS NULL
                GROUP BY role, status
                ORDER BY memberships DESC, role ASC, status ASC
                """
            )
            membership_roles = [dict(row) for row in role_rows]

        pending_invites = 0
        if tables["tenant_invites"]:
            pending_invites = _int(await conn.fetchval(
                "SELECT COUNT(*)::int FROM tenant_invites WHERE status = 'pending' AND deleted_at IS NULL AND expires_at > NOW()"
            ))

        oauth_7d = await _usage_totals(conn, "oauth_usage_log", 7)
        oauth_window = await _usage_totals(conn, "oauth_usage_log", days)
        bg_7d = await _usage_totals(conn, "bg_llm_usage_log", 7)
        bg_window = await _usage_totals(conn, "bg_llm_usage_log", days)
        chat = await _chat_totals(conn, days)

        plans = [
            {"plan": row["plan"], "users": _int(row["users"])}
            for row in await conn.fetch(
                f"""
                SELECT {plan_expr} AS plan, COUNT(*)::int AS users
                FROM saas_users
                WHERE {not_deleted_expr}
                GROUP BY 1
                ORDER BY users DESC, plan ASC
                """
            )
        ]

        tenants: list[dict[str, Any]] = []
        if tables["tenants"]:
            tenant_rows = await conn.fetch(
                """
                SELECT
                    t.id::text AS tenant_id,
                    t.slug,
                    t.name,
                    t.kind,
                    t.status,
                    t.created_at,
                    COUNT(DISTINCT tm.user_id) FILTER (WHERE tm.status = 'active' AND tm.deleted_at IS NULL)::int AS active_members,
                    COUNT(DISTINCT ti.id) FILTER (WHERE ti.status = 'pending' AND ti.deleted_at IS NULL AND ti.expires_at > NOW())::int AS pending_invites
                FROM tenants t
                LEFT JOIN tenant_memberships tm ON tm.tenant_id = t.id
                LEFT JOIN tenant_invites ti ON ti.tenant_id = t.id
                WHERE t.deleted_at IS NULL
                GROUP BY t.id, t.slug, t.name, t.kind, t.status, t.created_at
                ORDER BY active_members DESC, t.created_at DESC NULLS LAST
                LIMIT $1
                """,
                limit,
            )
            tenants = [
                {
                    "tenant_id": row["tenant_id"],
                    "slug": row["slug"] or "",
                    "name": row["name"] or "",
                    "kind": row["kind"] or "",
                    "status": row["status"] or "",
                    "active_members": _int(row["active_members"]),
                    "pending_invites": _int(row["pending_invites"]),
                    "created_at": _iso(row["created_at"]),
                }
                for row in tenant_rows
            ]

        daily_rows = await conn.fetch(
            """
            WITH days AS (
                SELECT generate_series(
                    (CURRENT_DATE - INTERVAL '13 days')::date,
                    CURRENT_DATE,
                    INTERVAL '1 day'
                )::date AS day
            ),
            signups AS (
                SELECT DATE(created_at AT TIME ZONE 'Asia/Seoul') AS day, COUNT(*)::int AS users
                FROM saas_users
                WHERE created_at >= CURRENT_DATE - INTERVAL '13 days'
                GROUP BY 1
            )
            SELECT TO_CHAR(d.day, 'YYYY-MM-DD') AS day, COALESCE(s.users, 0)::int AS signups
            FROM days d
            LEFT JOIN signups s ON s.day = d.day
            ORDER BY d.day ASC
            """
        )

        users = await _user_rows(conn, days, limit)

    summary = {
        "total_users": _int(user_summary["total_users"]),
        "active_users": _int(user_summary["active_users"]),
        "suspended_users": _int(user_summary["suspended_users"]),
        "deleted_users": _int(user_summary["deleted_users"]),
        "new_users_7d": _int(user_summary["new_users_7d"]),
        "new_users_30d": _int(user_summary["new_users_30d"]),
        "total_tenants": _int(tenant_summary.get("total_tenants")),
        "customer_tenants": _int(tenant_summary.get("customer_tenants")),
        "internal_tenants": _int(tenant_summary.get("internal_tenants")),
        "active_tenants": _int(tenant_summary.get("active_tenants")),
        "active_memberships": _int(membership_summary.get("active_memberships")),
        "internal_active_memberships": _int(membership_summary.get("internal_active_memberships")),
        "pending_invites": pending_invites,
        "calls_7d": _int(oauth_7d["calls"]) + _int(bg_7d["calls"]),
        "calls_window": _int(oauth_window["calls"]) + _int(bg_window["calls"]),
        "tokens_7d": _int(oauth_7d["tokens"]) + _int(bg_7d["tokens"]),
        "tokens_window": _int(oauth_window["tokens"]) + _int(bg_window["tokens"]),
        "usage_cost_7d": round(_float(oauth_7d["cost_usd"]) + _float(bg_7d["cost_usd"]), 6),
        "usage_cost_window": round(_float(oauth_window["cost_usd"]) + _float(bg_window["cost_usd"]), 6),
        "chat_sessions_window": chat["sessions"],
        "chat_messages_window": chat["messages"],
        "chat_tokens_window": chat["tokens"],
        "chat_cost_window": chat["cost_usd"],
    }

    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "summary": summary,
        "tables": tables,
        "plans": plans,
        "membership_roles": membership_roles,
        "tenants": tenants,
        "users": users,
        "daily": [{"day": row["day"], "signups": _int(row["signups"])} for row in daily_rows],
    }
