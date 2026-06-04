import os
import time
import hmac
import logging
from enum import Enum
from typing import Callable, Optional

import structlog
from fastapi import Depends, Header, HTTPException

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


# --- SaaS 회원 관리 ---

async def _get_pool():
    import asyncpg
    dsn = os.getenv('DATABASE_URL', 'postgresql://aads:aads@aads-postgres:5432/aads')
    return await asyncpg.create_pool(dsn, min_size=1, max_size=3)

_pool = None

async def _ensure_pool():
    global _pool
    if _pool is None:
        _pool = await _get_pool()
    return _pool


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
            UPDATE saas_users
               SET default_tenant_id = public.aads_internal_tenant_id()
             WHERE default_tenant_id IS NULL;
            ALTER TABLE saas_users
                ALTER COLUMN default_tenant_id SET DEFAULT public.aads_internal_tenant_id();
            ALTER TABLE saas_users
                ALTER COLUMN default_tenant_id SET NOT NULL;

            INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
            SELECT default_tenant_id,
                   id,
                   CASE WHEN role IN ('ceo', 'admin', 'owner') THEN 'owner' ELSE 'member' END,
                   'active'
              FROM saas_users
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


async def create_saas_user(email: str, password: str, name: Optional[str] = None) -> Optional[dict]:
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
                if row:
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


async def _load_tenant_context(user: dict, requested_tenant_id: Optional[str] = None) -> dict:
    user_id = str(user.get('user_id') or '').strip()
    if not user_id:
        raise HTTPException(status_code=401, detail='Invalid token subject')

    await ensure_saas_users_table()
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
                   tm.status AS membership_status
              FROM tenant_memberships tm
              JOIN tenants t ON t.id = tm.tenant_id
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
    }


# ── FastAPI Dependency: JWT에서 현재 사용자 추출 ─────────────────────
async def get_current_user(
    authorization: str = Header(None),
    x_tenant_id: Optional[str] = Header(None, alias='X-Tenant-ID'),
) -> dict:
    """Bearer 토큰에서 사용자 정보 추출. Depends()로 사용."""
    if not JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail='JWT not available')
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Authorization header missing')
    token = authorization[7:]
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail='Invalid token')
    current_user = {
        'user_id': payload.get('sub'),
        'email': payload.get('email', ''),
        'is_admin': payload.get('is_admin', False),
        'tenant_id': payload.get('tenant_id'),
    }
    context = await _load_tenant_context(current_user, requested_tenant_id=x_tenant_id)
    current_user['current_tenant'] = context['tenant']
    current_user['current_membership'] = context['membership']
    current_user['tenant_id'] = context['tenant']['id']
    current_user['tenant_role'] = context['membership']['role']
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
