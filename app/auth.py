import os
import time
import hmac
import logging
import hashlib
import re
import secrets
from enum import Enum
from datetime import datetime, timezone
from typing import Callable, Optional

import structlog
from fastapi import Depends, Header, HTTPException, Request

log = structlog.get_logger()

try:
    import jwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    log.warning('pyjwt_not_installed', detail='auth endpoints will return 503')

try:
    import bcrypt as _bcrypt_mod
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False
    _bcrypt_mod = None
    log.warning('bcrypt_not_installed', detail='SaaS registration will be unavailable')

SECRET_KEY = os.getenv('JWT_SECRET_KEY', '')
ALGORITHM = 'HS256'
TOKEN_EXPIRE_HOURS = 24 * 7  # 7일

ADMIN_EMAIL = os.getenv('AADS_ADMIN_EMAIL', 'admin@aads.dev')
ADMIN_PASSWORD = os.getenv('AADS_ADMIN_PASSWORD', '')
INTERNAL_TENANT_ALLOWED_ROLES = {'ceo', 'admin', 'system'}


class TenantRole(str, Enum):
    OWNER = 'owner'
    ADMIN = 'admin'
    MEMBER = 'member'
    VIEWER = 'viewer'


TENANT_ROLE_RANK = {
    TenantRole.VIEWER: 10,
    TenantRole.MEMBER: 20,
    TenantRole.ADMIN: 30,
    TenantRole.OWNER: 40,
}

if not SECRET_KEY:
    raise RuntimeError(
        'JWT_SECRET_KEY environment variable is not set. '
        'Set it in .env before starting the server.'
    )

if not ADMIN_PASSWORD:
    log.warning('admin_password_not_set', detail='Auth endpoints will return 503 until AADS_ADMIN_PASSWORD is set')


def create_token(user_id: str, email: str, *, is_admin: bool = False, tenant_id: Optional[str] = None) -> str:
    if not JWT_AVAILABLE:
        raise RuntimeError('PyJWT not installed')
    # PyJWT requires sub to be a string (not int from DB)
    payload = {
        'sub': str(user_id),
        'email': email,
        'is_admin': is_admin,
        'tenant_id': tenant_id,
        'iat': int(time.time()),
        'exp': int(time.time()) + TOKEN_EXPIRE_HOURS * 3600,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    if not JWT_AVAILABLE:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except Exception as e:
        log.debug('token_verification_failed', error=str(e))
        return None


def check_admin_credentials(email: str, password: str) -> bool:
    if not ADMIN_PASSWORD:
        return False
    email_ok = hmac.compare_digest(email.encode(), ADMIN_EMAIL.encode())
    pwd_ok = hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    return email_ok and pwd_ok


def normalize_tenant_role(role: Optional[str]) -> Optional[TenantRole]:
    try:
        return TenantRole(str(role or '').strip().lower())
    except ValueError:
        return None


def tenant_role_allows(role: Optional[str], minimum: TenantRole) -> bool:
    normalized = normalize_tenant_role(role)
    if not normalized:
        return False
    return TENANT_ROLE_RANK[normalized] >= TENANT_ROLE_RANK[minimum]


def _tenant_role_from_user_role(user_role: Optional[str]) -> str:
    role = str(user_role or '').strip().lower()
    if role in ('ceo', 'owner', 'admin'):
        return TenantRole.OWNER.value
    return TenantRole.MEMBER.value


def _internal_tenant_allowlist_emails() -> set[str]:
    configured = os.getenv('AADS_INTERNAL_TENANT_ALLOWLIST_EMAILS', '')
    emails = {
        _normalize_email(email)
        for email in configured.split(',')
        if _normalize_email(email)
    }
    if ADMIN_EMAIL:
        emails.add(_normalize_email(ADMIN_EMAIL))
    return emails


def _is_internal_tenant_principal(email: Optional[str], role: Optional[str]) -> bool:
    normalized_role = str(role or '').strip().lower()
    if normalized_role in INTERNAL_TENANT_ALLOWED_ROLES:
        return True
    normalized_email = _normalize_email(email or '')
    return bool(normalized_email and normalized_email in _internal_tenant_allowlist_emails())


# --- SaaS 회원 관리 ---

async def _get_pool():
    import asyncpg
    dsn = os.getenv('DATABASE_URL', 'postgresql://aads:aads@aads-postgres:5432/aads')
    return await asyncpg.create_pool(dsn, min_size=1, max_size=3)

_pool = None
_saas_schema_ready = False
_TENANT_SLUG_PATTERN = re.compile(r"[^a-z0-9-]+")

async def _ensure_pool():
    global _pool
    if _pool is None:
        _pool = await _get_pool()
    return _pool


async def require_saas_schema_ready() -> None:
    """Validate SaaS tenant schema without running request-time DDL."""
    global _saas_schema_ready
    if _saas_schema_ready:
        return

    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                to_regclass('public.saas_users') IS NOT NULL AS has_saas_users,
                to_regclass('public.tenants') IS NOT NULL AS has_tenants,
                to_regclass('public.tenant_memberships') IS NOT NULL AS has_tenant_memberships,
                to_regclass('public.tenant_invites') IS NOT NULL AS has_tenant_invites,
                EXISTS (
                    SELECT 1
                      FROM pg_proc p
                      JOIN pg_namespace n ON n.oid = p.pronamespace
                     WHERE n.nspname = 'public'
                       AND p.proname = 'aads_internal_tenant_id'
                ) AS has_internal_tenant_fn,
                EXISTS (
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name = 'saas_users'
                       AND column_name IN ('default_tenant_id', 'status', 'deleted_at', 'role')
                     GROUP BY table_name
                    HAVING COUNT(DISTINCT column_name) = 4
                ) AS has_saas_user_columns,
                EXISTS (
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name = 'tenant_memberships'
                       AND column_name IN ('tenant_id', 'user_id', 'role', 'status', 'deleted_at')
                     GROUP BY table_name
                    HAVING COUNT(DISTINCT column_name) = 5
                ) AS has_membership_columns
            """
        )
        missing = [
            name
            for name in (
                "has_saas_users",
                "has_tenants",
                "has_tenant_memberships",
                "has_tenant_invites",
                "has_internal_tenant_fn",
                "has_saas_user_columns",
                "has_membership_columns",
            )
            if not row or not row[name]
        ]
        if missing:
            log.error("saas_schema_not_ready", missing=missing)
            raise HTTPException(status_code=503, detail="SaaS schema is not initialized")

        has_internal_tenant = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM public.tenants WHERE slug = 'internal' AND deleted_at IS NULL)"
        )
        if not has_internal_tenant:
            log.error("saas_internal_tenant_missing")
            raise HTTPException(status_code=503, detail="Internal tenant is not initialized")

    _saas_schema_ready = True


async def ensure_saas_users_table():
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE EXTENSION IF NOT EXISTS pgcrypto;

            CREATE TABLE IF NOT EXISTS saas_users (
                id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            ALTER TABLE saas_users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user';

            CREATE TABLE IF NOT EXISTS tenants (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'customer'
                    CHECK (kind IN ('internal', 'customer')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'suspended', 'archived')),
                metadata JSONB NOT NULL DEFAULT '{}',
                created_by TEXT REFERENCES saas_users(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                deleted_at TIMESTAMPTZ
            );

            INSERT INTO tenants (slug, name, kind, status, metadata)
            VALUES ('internal', 'AADS Internal', 'internal', 'active', '{"runtime_bootstrap":true}'::jsonb)
            ON CONFLICT (slug) DO UPDATE
               SET status = 'active',
                   deleted_at = NULL,
                   updated_at = now();

            CREATE OR REPLACE FUNCTION public.aads_internal_tenant_id()
            RETURNS UUID
            LANGUAGE SQL
            STABLE
            AS $$
                SELECT id
                  FROM public.tenants
                 WHERE slug = 'internal'
                   AND deleted_at IS NULL
                 LIMIT 1
            $$;

            CREATE TABLE IF NOT EXISTS tenant_memberships (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'member'
                    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'invited', 'suspended', 'removed')),
                invited_by TEXT REFERENCES saas_users(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                deleted_at TIMESTAMPTZ,
                UNIQUE (tenant_id, user_id)
            );

            ALTER TABLE saas_users ADD COLUMN IF NOT EXISTS default_tenant_id UUID;
            ALTER TABLE saas_users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'deleted'));
            ALTER TABLE saas_users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
            ALTER TABLE saas_users
                ALTER COLUMN default_tenant_id DROP DEFAULT;
            ALTER TABLE saas_users
                ALTER COLUMN default_tenant_id DROP NOT NULL;
            UPDATE saas_users
               SET default_tenant_id = public.aads_internal_tenant_id()
             WHERE default_tenant_id IS NULL
               AND role IN ('ceo', 'admin', 'system');

            INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
            SELECT default_tenant_id,
                   id,
                   CASE WHEN role IN ('ceo', 'admin', 'system') THEN 'owner' ELSE 'member' END,
                   'active'
              FROM saas_users
             WHERE role IN ('ceo', 'admin', 'system')
               AND default_tenant_id IS NOT NULL
            ON CONFLICT (tenant_id, user_id) DO UPDATE
               SET status = 'active',
                   role = CASE
                       WHEN tenant_memberships.role = 'owner' THEN 'owner'
                       WHEN EXCLUDED.role = 'owner' THEN 'owner'
                       ELSE tenant_memberships.role
                   END,
                   deleted_at = NULL,
                   updated_at = now();
        """)


async def create_saas_user(
    email: str,
    password: str,
    name: Optional[str] = None,
    *,
    attach_internal_tenant: bool = False,
) -> Optional[dict]:
    if not BCRYPT_AVAILABLE:
        log.error('bcrypt_unavailable', detail='bcrypt not installed')
        return None
    try:
        password_hash = _bcrypt_mod.hashpw(password.encode('utf-8'), _bcrypt_mod.gensalt()).decode('utf-8')
        pool = await _ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """INSERT INTO saas_users (email, password_hash, name)
                       VALUES ($1, $2, $3)
                       RETURNING id, email, name, default_tenant_id, created_at""",
                    email, password_hash, name
                )
                if row and attach_internal_tenant:
                    await conn.execute(
                        """INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
                           VALUES ($1, $2, 'member', 'active')
                           ON CONFLICT (tenant_id, user_id) DO UPDATE
                              SET status = 'active',
                                  deleted_at = NULL,
                                  updated_at = now()""",
                        row['default_tenant_id'],
                        row['id'],
                    )
            return dict(row) if row else None
    except Exception as e:
        log.error('create_saas_user_failed', error=str(e))
        return None


async def authenticate_saas_user(email: str, password: str) -> Optional[dict]:
    if not BCRYPT_AVAILABLE:
        return None
    try:
        pool = await _ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, email, name, password_hash, default_tenant_id, role
                     FROM saas_users
                    WHERE email = $1
                      AND COALESCE(status, 'active') = 'active'
                      AND deleted_at IS NULL""",
                email
            )
            if not row:
                return None
            if _bcrypt_mod.checkpw(password.encode('utf-8'), row['password_hash'].encode('utf-8')):
                return {
                    'id': row['id'],
                    'email': row['email'],
                    'name': row['name'],
                    'tenant_id': str(row['default_tenant_id']) if row['default_tenant_id'] else None,
                    'role': row['role'],
                }
            return None
    except Exception as e:
        log.error('authenticate_saas_user_failed', error=str(e))
        return None


async def get_saas_user_by_email(email: str) -> Optional[dict]:
    try:
        pool = await _ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, email, name, default_tenant_id, role
                     FROM saas_users
                    WHERE email = $1
                      AND deleted_at IS NULL""",
                email
            )
            return dict(row) if row else None
    except Exception as e:
        log.error('get_saas_user_failed', error=str(e))
        return None


async def get_internal_tenant_id() -> Optional[str]:
    try:
        pool = await _ensure_pool()
        async with pool.acquire() as conn:
            tenant_id = await conn.fetchval("SELECT public.aads_internal_tenant_id()")
            return str(tenant_id) if tenant_id else None
    except Exception as e:
        log.error('get_internal_tenant_failed', error=str(e))
        return None


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _normalize_tenant_slug(value: str) -> str:
    slug = _TENANT_SLUG_PATTERN.sub("-", str(value or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:64] or "tenant"


def _hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def list_user_tenants(user_id: str) -> list[dict]:
    await require_saas_schema_ready()
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.id::text AS tenant_id,
                   t.slug,
                   t.name,
                   t.kind,
                   t.status,
                   t.metadata,
                   tm.role,
                   tm.status AS membership_status,
                   tm.created_at,
                   tm.updated_at,
                   u.email AS user_email,
                   u.role AS user_role
              FROM tenant_memberships tm
              JOIN tenants t ON t.id = tm.tenant_id
              JOIN saas_users u ON u.id = tm.user_id
             WHERE tm.user_id = $1
               AND tm.status = 'active'
               AND tm.deleted_at IS NULL
               AND t.deleted_at IS NULL
             ORDER BY t.kind = 'internal' DESC, t.created_at ASC
            """,
            user_id,
        )
    tenants: list[dict] = []
    for row in rows:
        tenant = dict(row)
        if str(tenant.get("kind") or "").lower() == "internal":
            if str(tenant.get("role") or "").lower() not in {TenantRole.OWNER.value, TenantRole.ADMIN.value}:
                continue
            if not _is_internal_tenant_principal(tenant.get("user_email"), tenant.get("user_role")):
                continue
        tenant.pop("user_email", None)
        tenant.pop("user_role", None)
        tenants.append(tenant)
    return tenants


async def create_tenant_for_user(
    *,
    user_id: str,
    name: str,
    slug: Optional[str] = None,
    plan_key: str = "free",
) -> dict:
    await require_saas_schema_ready()
    tenant_name = str(name or "").strip()
    if not tenant_name:
        raise HTTPException(status_code=422, detail="Tenant name is required")

    base_slug = _normalize_tenant_slug(slug or tenant_name)
    plan = str(plan_key or "free").strip().lower()
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user_exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM saas_users
                     WHERE id = $1::text AND COALESCE(status, 'active') = 'active' AND deleted_at IS NULL
                )
                """,
                user_id,
            )
            if not user_exists:
                raise HTTPException(status_code=404, detail="User not found")

            slug_candidate = base_slug
            for suffix in range(0, 100):
                if suffix:
                    slug_candidate = f"{base_slug}-{suffix + 1}"
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM tenants WHERE slug = $1 AND deleted_at IS NULL)",
                    slug_candidate,
                )
                if not exists:
                    break
            else:
                raise HTTPException(status_code=409, detail="Tenant slug is unavailable")

            tenant = await conn.fetchrow(
                """
                INSERT INTO tenants (slug, name, kind, status, metadata, created_by)
                VALUES ($1::text, $2::text, 'customer', 'active', jsonb_build_object('plan_key', $3::text), $4::text)
                RETURNING id::text AS tenant_id, slug, name, kind, status, metadata, created_at
                """,
                slug_candidate,
                tenant_name,
                plan,
                user_id,
            )
            membership = await conn.fetchrow(
                """
                INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
                VALUES ($1::uuid, $2, 'owner', 'active')
                ON CONFLICT (tenant_id, user_id) DO UPDATE
                   SET role = 'owner',
                       status = 'active',
                       deleted_at = NULL,
                       updated_at = now()
                RETURNING id::text AS membership_id, role, status
                """,
                tenant["tenant_id"],
                user_id,
            )
            await conn.execute(
                "UPDATE saas_users SET default_tenant_id = $1::uuid, updated_at = now() WHERE id = $2::text",
                tenant["tenant_id"],
                user_id,
            )
    out = dict(tenant)
    out["membership"] = dict(membership) if membership else None
    out["workspace"] = await ensure_default_customer_workspace(
        tenant_id=str(tenant["tenant_id"]),
        tenant_name=str(tenant["name"]),
    )
    return out


async def ensure_default_customer_workspace(*, tenant_id: str, tenant_name: str = "") -> Optional[dict]:
    """Ensure a customer tenant has an isolated chat workspace.

    New SaaS tenants only had tenant_memberships, so the chat UI had no workspace
    to create sessions under. Keep this helper in auth.py to avoid importing the
    large chat service during signup/onboarding.
    """
    await require_saas_schema_ready()
    tenant_uuid = str(tenant_id or "").strip()
    if not tenant_uuid:
        return None
    display_name = str(tenant_name or "내 작업공간").strip() or "내 작업공간"
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            """
            SELECT id::text AS tenant_id, name, kind
              FROM tenants
             WHERE id = $1::uuid
               AND status = 'active'
               AND deleted_at IS NULL
            """,
            tenant_uuid,
        )
        if not tenant or str(tenant["kind"]).lower() != "customer":
            return None
        existing = await conn.fetchrow(
            """
            SELECT id::text, name
              FROM chat_workspaces
             WHERE tenant_id = $1::uuid
             ORDER BY created_at ASC
             LIMIT 1
            """,
            tenant_uuid,
        )
        if existing:
            return dict(existing)
        row = await conn.fetchrow(
            """
            INSERT INTO chat_workspaces (tenant_id, name, system_prompt, files, settings, color, icon)
            VALUES (
                $1::uuid,
                $2,
                $3,
                '[]'::jsonb,
                jsonb_build_object(
                    'project_key', 'CUSTOMER',
                    'default_role_key', 'GeneralAssistant',
                    'allowed_roles', ARRAY['GeneralAssistant']::text[],
                    'role_routing_enabled', false,
                    'customer_default', true
                ),
                '#2563EB',
                '💬'
            )
            RETURNING id::text, name
            """,
            tenant_uuid,
            f"[WORK] {display_name}",
            (
                "이 워크스페이스는 고객 tenant 전용 작업공간입니다. "
                "답변은 이 조직의 프로젝트, 팀, 산출물, 사용량 범위로 제한하고 "
                "AADS 내부 운영/CEO 프로젝트를 기본 안내하지 마세요."
            ),
        )
    return dict(row) if row else None


async def finalize_customer_tenant_onboarding(
    *,
    user_id: str,
    tenant_id: str,
    name: str,
) -> dict:
    await require_saas_schema_ready()
    tenant_name = str(name or "").strip()
    if not tenant_name:
        raise HTTPException(status_code=422, detail="Tenant name is required")

    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT t.id::text AS tenant_id,
                       t.slug,
                       t.name,
                       t.kind,
                       t.status,
                       t.metadata,
                       tm.id::text AS membership_id,
                       tm.role,
                       tm.status AS membership_status
                  FROM tenant_memberships tm
                  JOIN tenants t ON t.id = tm.tenant_id
                 WHERE tm.user_id = $1
                   AND tm.tenant_id = $2::uuid
                   AND tm.status = 'active'
                   AND tm.deleted_at IS NULL
                   AND t.kind = 'customer'
                   AND t.status = 'active'
                   AND t.deleted_at IS NULL
                 LIMIT 1
                """,
                user_id,
                tenant_id,
            )
            if not row:
                raise HTTPException(status_code=403, detail="Customer tenant membership required")
            if str(row["role"]).lower() not in {TenantRole.OWNER.value, TenantRole.ADMIN.value}:
                raise HTTPException(status_code=403, detail="Tenant admin role required")

            tenant = await conn.fetchrow(
                """
                UPDATE tenants
                   SET name = $1,
                       updated_at = now()
                 WHERE id = $2::uuid
                RETURNING id::text AS tenant_id, slug, name, kind, status, metadata, created_at
                """,
                tenant_name,
                tenant_id,
            )
            await conn.execute(
                "UPDATE saas_users SET default_tenant_id = $1::uuid, updated_at = now() WHERE id = $2",
                tenant_id,
                user_id,
            )

    out = dict(tenant)
    out["membership"] = {
        "membership_id": row["membership_id"],
        "role": row["role"],
        "status": row["membership_status"],
    }
    out["workspace"] = await ensure_default_customer_workspace(
        tenant_id=str(tenant["tenant_id"]),
        tenant_name=str(tenant["name"]),
    )
    return out


async def ensure_customer_tenant_for_user(
    *,
    user_id: str,
    email: str,
    name: Optional[str] = None,
    plan_key: str = "free",
) -> dict:
    """Return an active customer tenant for a SaaS user, creating one if needed."""
    await require_saas_schema_ready()
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT t.id::text AS tenant_id,
                   t.slug,
                   t.name,
                   t.kind,
                   t.status,
                   t.metadata,
                   tm.id::text AS membership_id,
                   tm.role,
                   tm.status AS membership_status
              FROM tenant_memberships tm
              JOIN tenants t ON t.id = tm.tenant_id
             WHERE tm.user_id = $1
               AND tm.status = 'active'
               AND tm.deleted_at IS NULL
               AND t.kind = 'customer'
               AND t.status = 'active'
               AND t.deleted_at IS NULL
             ORDER BY tm.created_at ASC
             LIMIT 1
            """,
            user_id,
        )
        if existing:
            await conn.execute(
                "UPDATE saas_users SET default_tenant_id = $1::uuid, updated_at = now() WHERE id = $2",
                existing["tenant_id"],
                user_id,
            )
            out = dict(existing)
            out["workspace"] = await ensure_default_customer_workspace(
                tenant_id=str(existing["tenant_id"]),
                tenant_name=str(existing["name"]),
            )
            return out

    workspace_name = (name and f"{name} Workspace") or f"{str(email).split('@')[0]} Workspace"
    return await create_tenant_for_user(
        user_id=user_id,
        name=workspace_name,
        plan_key=plan_key,
    )


async def resolve_login_tenant_for_user(user: dict) -> Optional[str]:
    """Return the tenant a SaaS user should start in after login."""
    user_id = str(user.get("id") or user.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user")

    default_tenant_id = str(user.get("tenant_id") or user.get("default_tenant_id") or "").strip() or None
    if _is_internal_tenant_principal(user.get("email"), user.get("role")):
        for tenant in await list_user_tenants(user_id):
            if str(tenant.get("kind") or "").lower() == "internal":
                return str(tenant.get("tenant_id") or "") or default_tenant_id
        if default_tenant_id:
            return default_tenant_id

    tenant = await ensure_customer_tenant_for_user(
        user_id=user_id,
        email=str(user.get("email") or ""),
        name=user.get("name"),
        plan_key="free",
    )
    return str(tenant.get("tenant_id") or "") or None


async def switch_user_tenant(user_id: str, tenant_id: str) -> dict:
    context = await _load_tenant_context({"user_id": user_id}, requested_tenant_id=tenant_id)
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE saas_users SET default_tenant_id = $1::uuid, updated_at = now() WHERE id = $2",
            context["tenant"]["id"],
            user_id,
        )
        user = await conn.fetchrow(
            "SELECT id, email, name FROM saas_users WHERE id = $1 AND deleted_at IS NULL",
            user_id,
        )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user": dict(user), "context": context}


async def create_tenant_invite(
    *,
    tenant_id: str,
    email: str,
    role: str,
    invited_by: str,
    expires_in_hours: int = 24 * 7,
) -> dict:
    await require_saas_schema_ready()
    invite_role = normalize_tenant_role(role)
    if invite_role not in {TenantRole.ADMIN, TenantRole.MEMBER, TenantRole.VIEWER}:
        raise HTTPException(status_code=422, detail="Invite role must be admin, member, or viewer")

    token = secrets.token_urlsafe(32)
    token_hash = _hash_invite_token(token)
    normalized_email = _normalize_email(email)
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        tenant_kind = await conn.fetchval(
            """
            SELECT kind
              FROM tenants
             WHERE id = $1::uuid
               AND status = 'active'
               AND deleted_at IS NULL
            """,
            tenant_id,
        )
        if not tenant_kind:
            raise HTTPException(status_code=404, detail="Tenant not found")
        if str(tenant_kind).lower() == "internal":
            raise HTTPException(status_code=403, detail="Internal tenant invites are restricted")
        row = await conn.fetchrow(
            """
            INSERT INTO tenant_invites
                (tenant_id, email, token_hash, role, status, invited_by, expires_at)
            VALUES ($1::uuid, $2, $3, $4, 'pending', $5, now() + ($6::text || ' hours')::interval)
            ON CONFLICT (tenant_id, lower(email)) WHERE status = 'pending' AND deleted_at IS NULL
            DO UPDATE SET token_hash = EXCLUDED.token_hash,
                          role = EXCLUDED.role,
                          invited_by = EXCLUDED.invited_by,
                          expires_at = EXCLUDED.expires_at,
                          updated_at = now()
            RETURNING id::text AS invite_id, tenant_id::text, email, role, status, expires_at, created_at
            """,
            tenant_id,
            normalized_email,
            token_hash,
            invite_role.value,
            invited_by,
            max(1, min(int(expires_in_hours or 1), 24 * 30)),
        )
    result = dict(row)
    result["token"] = token
    return result


async def list_tenant_members(tenant_id: str) -> list[dict]:
    await require_saas_schema_ready()
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tm.id::text AS membership_id,
                   tm.tenant_id::text,
                   tm.user_id,
                   tm.role,
                   tm.status,
                   tm.created_at,
                   tm.updated_at,
                   u.email,
                   u.name
              FROM tenant_memberships tm
              JOIN saas_users u ON u.id = tm.user_id
             WHERE tm.tenant_id = $1::uuid
               AND tm.deleted_at IS NULL
               AND u.deleted_at IS NULL
             ORDER BY
                   CASE tm.role
                       WHEN 'owner' THEN 1
                       WHEN 'admin' THEN 2
                       WHEN 'member' THEN 3
                       ELSE 4
                   END,
                   lower(u.email)
            """,
            tenant_id,
        )
    return [dict(row) for row in rows]


async def list_tenant_pending_invites(tenant_id: str) -> list[dict]:
    await require_saas_schema_ready()
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ti.id::text AS invite_id,
                   ti.tenant_id::text,
                   ti.email,
                   ti.role,
                   ti.status,
                   ti.expires_at,
                   ti.created_at,
                   ti.updated_at,
                   u.email AS invited_by_email
              FROM tenant_invites ti
              LEFT JOIN saas_users u ON u.id = ti.invited_by
             WHERE ti.tenant_id = $1::uuid
               AND ti.status = 'pending'
               AND ti.deleted_at IS NULL
             ORDER BY ti.created_at DESC
            """,
            tenant_id,
        )
    return [dict(row) for row in rows]


async def accept_tenant_invite(
    *,
    token: str,
    password: str,
    name: Optional[str] = None,
) -> dict:
    await require_saas_schema_ready()
    if not token:
        raise HTTPException(status_code=422, detail="Invite token is required")
    if not password:
        raise HTTPException(status_code=422, detail="Password is required")

    token_hash = _hash_invite_token(token)
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        invite = await conn.fetchrow(
            """
            SELECT id::text AS invite_id,
                   tenant_id::text,
                   email,
                   role,
                   status,
                   expires_at
              FROM tenant_invites
             WHERE token_hash = $1
               AND status = 'pending'
               AND deleted_at IS NULL
             LIMIT 1
            """,
            token_hash,
        )
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    expires_at = invite["expires_at"]
    if expires_at and expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE tenant_invites SET status = 'expired', updated_at = now() WHERE id = $1::uuid",
                invite["invite_id"],
            )
        raise HTTPException(status_code=410, detail="Invite expired")

    email = str(invite["email"])
    user = await get_saas_user_by_email(email)
    if user:
        authed = await authenticate_saas_user(email, password)
        if not authed:
            raise HTTPException(status_code=401, detail="Password is required for existing user")
        user_id = str(authed["id"])
    else:
        created = await create_saas_user(email, password, name, attach_internal_tenant=False)
        if not created:
            raise HTTPException(status_code=500, detail="Unable to create invited user")
        user_id = str(created["id"])

    async with pool.acquire() as conn:
        async with conn.transaction():
            membership = await conn.fetchrow(
                """
                INSERT INTO tenant_memberships (tenant_id, user_id, role, status, invited_by)
                SELECT id.tenant_id::uuid, $2, id.role, 'active', ti.invited_by
                  FROM (SELECT $1::uuid AS tenant_id, $3::text AS role) id
                  JOIN tenant_invites ti ON ti.id = $4::uuid
                ON CONFLICT (tenant_id, user_id) DO UPDATE
                   SET role = EXCLUDED.role,
                       status = 'active',
                       deleted_at = NULL,
                       updated_at = now()
                RETURNING id::text AS membership_id, tenant_id::text, role, status
                """,
                invite["tenant_id"],
                user_id,
                invite["role"],
                invite["invite_id"],
            )
            await conn.execute(
                """
                UPDATE tenant_invites
                   SET status = 'accepted',
                       accepted_by = $1,
                       accepted_at = now(),
                       updated_at = now()
                 WHERE id = $2::uuid
                """,
                user_id,
                invite["invite_id"],
            )
            await conn.execute(
                "UPDATE saas_users SET default_tenant_id = $1::uuid, updated_at = now() WHERE id = $2",
                invite["tenant_id"],
                user_id,
            )
            user_row = await conn.fetchrow(
                "SELECT id, email, name FROM saas_users WHERE id = $1",
                user_id,
            )
    workspace = await ensure_default_customer_workspace(
        tenant_id=str(invite["tenant_id"]),
        tenant_name="팀 작업공간",
    )
    return {
        "user": dict(user_row) if user_row else {"id": user_id, "email": email, "name": name},
        "tenant_id": invite["tenant_id"],
        "membership": dict(membership) if membership else None,
        "workspace": workspace,
    }


async def update_tenant_plan(tenant_id: str, plan_key: str, updated_by: str) -> dict:
    await require_saas_schema_ready()
    plan = str(plan_key or "").strip().lower()
    if not plan:
        raise HTTPException(status_code=422, detail="plan_key is required")
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        plan_exists = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM tenant_plan_limits
                 WHERE plan_key = $1 AND is_active = TRUE
            )
            """,
            plan,
        )
        if not plan_exists:
            raise HTTPException(status_code=404, detail="Plan not found")
        row = await conn.fetchrow(
            """
            UPDATE tenants
               SET metadata = jsonb_set(
                       COALESCE(metadata, '{}'::jsonb),
                       '{plan_key}',
                       to_jsonb($2::text),
                       true
                   ) || jsonb_build_object('plan_updated_by', $3, 'plan_updated_at', now()::text),
                   updated_at = now()
             WHERE id = $1::uuid
               AND deleted_at IS NULL
             RETURNING id::text AS tenant_id, slug, name, kind, status, metadata
            """,
            tenant_id,
            plan,
            updated_by,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return dict(row)


async def _load_tenant_context(user: dict, requested_tenant_id: Optional[str] = None) -> dict:
    user_id = str(user.get('user_id') or '').strip()
    if not user_id:
        raise HTTPException(status_code=401, detail='Invalid token subject')

    await require_saas_schema_ready()
    token_tenant_id = str(user.get('tenant_id') or '').strip() or None
    tenant_id = str(requested_tenant_id or '').strip() or token_tenant_id

    if user.get('is_admin'):
        internal_tenant_id = await get_internal_tenant_id()
        if tenant_id and internal_tenant_id and tenant_id != internal_tenant_id:
            raise HTTPException(status_code=403, detail='Tenant access denied')
        tenant_id = internal_tenant_id
        if not tenant_id:
            raise HTTPException(status_code=503, detail='Internal tenant unavailable')
        return {
            'tenant': {
                'id': tenant_id,
                'slug': 'internal',
                'name': 'AADS Internal',
                'kind': 'internal',
                'status': 'active',
            },
            'membership': {
                'tenant_id': tenant_id,
                'user_id': user_id,
                'role': TenantRole.OWNER.value,
                'status': 'active',
            },
            'user_role': 'system',
        }

    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        if not tenant_id:
            tenant_id = await conn.fetchval(
                """SELECT default_tenant_id::text
                     FROM saas_users
                    WHERE id = $1
                      AND COALESCE(status, 'active') = 'active'
                      AND deleted_at IS NULL""",
                user_id,
            )
        row = await conn.fetchrow(
            """
            SELECT t.id::text AS tenant_id,
                   t.slug,
                   t.name,
                   t.kind,
                   t.status AS tenant_status,
                   tm.id::text AS membership_id,
                   tm.role,
                   tm.status AS membership_status,
                   u.email AS user_email,
                   u.role AS user_role
              FROM tenant_memberships tm
              JOIN tenants t ON t.id = tm.tenant_id
              JOIN saas_users u ON u.id = tm.user_id
             WHERE tm.user_id = $1
               AND tm.tenant_id = $2::uuid
               AND tm.status = 'active'
               AND tm.deleted_at IS NULL
               AND t.status = 'active'
               AND t.deleted_at IS NULL
             LIMIT 1
            """,
            user_id,
            tenant_id,
        )
    if not row:
        raise HTTPException(status_code=403, detail='Tenant membership required')
    if str(row['kind']).lower() == 'internal':
        if str(row['role']).lower() not in {'owner', 'admin'}:
            raise HTTPException(status_code=403, detail='Internal tenant requires admin role')
        if not _is_internal_tenant_principal(row['user_email'], row['user_role']):
            raise HTTPException(status_code=403, detail='Internal tenant requires CEO/admin/system allowlist')
    return {
        'tenant': {
            'id': row['tenant_id'],
            'slug': row['slug'],
            'name': row['name'],
            'kind': row['kind'],
            'status': row['tenant_status'],
        },
        'membership': {
            'id': row['membership_id'],
            'tenant_id': row['tenant_id'],
            'user_id': user_id,
            'role': row['role'],
            'status': row['membership_status'],
        },
        'user_role': row['user_role'],
    }


# ── FastAPI Dependency: JWT에서 현재 사용자 추출 ─────────────────────
async def get_current_user(
    request: Request,
    authorization: str = Header(None),
    x_tenant_id: Optional[str] = Header(None, alias='X-Tenant-ID'),
    x_monitor_key: Optional[str] = Header(None, alias='x-monitor-key'),
) -> dict:
    """Bearer 토큰에서 사용자 정보 추출. Depends()로 사용."""
    if not JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail='JWT not available')
    monitor_key = (
        x_monitor_key
        or request.headers.get('x-monitor-key')
        or request.headers.get('X-Monitor-Key')
        or ''
    ).strip()
    request_path = request.url.path or ''
    if (
        monitor_key == 'internal-pipeline-call'
        and (
            request_path.startswith(('/api/v1/pipeline/', '/pipeline/'))
            or '/pipeline/' in request_path
        )
    ):
        pool = await _ensure_pool()
        async with pool.acquire() as conn:
            tenant = await conn.fetchrow(
                """
                SELECT id::text AS id, slug, name, kind, status
                  FROM tenants
                 WHERE slug = 'internal'
                   AND deleted_at IS NULL
                 LIMIT 1
                """
            )
        if not tenant:
            raise HTTPException(status_code=503, detail='Internal tenant is not initialized')
        membership = {
            'id': 'internal-pipeline-call',
            'tenant_id': tenant['id'],
            'user_id': 'system:pipeline-runner',
            'role': TenantRole.OWNER.value,
            'status': 'active',
        }
        return {
            'user_id': 'system:pipeline-runner',
            'email': 'system@aads.internal',
            'is_admin': True,
            'tenant_id': tenant['id'],
            'current_tenant': {
                'id': tenant['id'],
                'slug': tenant['slug'],
                'name': tenant['name'],
                'kind': tenant['kind'],
                'status': tenant['status'],
            },
            'current_membership': membership,
            'tenant_role': TenantRole.OWNER.value,
            'user_role': 'system',
            'is_internal_admin': True,
        }
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Authorization header missing')
    token = authorization[7:]
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail='Invalid token')
    email = payload.get('email', '')
    is_admin_principal = bool(payload.get('is_admin', False)) or (
        _normalize_email(email) == _normalize_email(ADMIN_EMAIL)
    )
    current_user = {
        'user_id': payload.get('sub'),
        'email': email,
        'is_admin': is_admin_principal,
        'tenant_id': payload.get('tenant_id'),
    }
    context = await _load_tenant_context(current_user, requested_tenant_id=x_tenant_id)
    current_user['current_tenant'] = context['tenant']
    current_user['current_membership'] = context['membership']
    current_user['tenant_id'] = context['tenant']['id']
    current_user['tenant_role'] = context['membership']['role']
    current_user['user_role'] = context.get('user_role')
    internal_principal = _is_internal_tenant_principal(current_user.get('email'), context.get('user_role'))
    current_user['is_internal_admin'] = bool(
        current_user.get('is_admin')
        or internal_principal
        or (
            str(context['tenant'].get('kind') or '').lower() == 'internal'
            and str(context['membership'].get('role') or '').lower() in {TenantRole.OWNER.value, TenantRole.ADMIN.value}
            and internal_principal
        )
    )
    return current_user


async def get_current_tenant_context(current_user: dict = Depends(get_current_user)) -> dict:
    return {
        'user': current_user,
        'tenant': current_user['current_tenant'],
        'membership': current_user['current_membership'],
    }


def require_tenant_role(minimum: TenantRole) -> Callable:
    async def _dependency(context: dict = Depends(get_current_tenant_context)) -> dict:
        role = context.get('membership', {}).get('role')
        if not tenant_role_allows(role, minimum):
            raise HTTPException(status_code=403, detail=f'{minimum.value} role required')
        return context

    return _dependency


async def require_internal_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """CEO/admin/system allowlist 전용 API 보호."""
    if not current_user.get('is_internal_admin'):
        raise HTTPException(status_code=403, detail='Internal admin access required')
    return current_user
