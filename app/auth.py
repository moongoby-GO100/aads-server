import os
import time
import hmac
import logging
from typing import Optional

import structlog
from fastapi import Header

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
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );

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
            SELECT default_tenant_id, id, 'member', 'active'
              FROM saas_users
            ON CONFLICT (tenant_id, user_id) DO NOTHING;
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
                """SELECT id, email, name, password_hash, default_tenant_id
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
                """SELECT id, email, name, default_tenant_id
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


# ── FastAPI Dependency: JWT에서 현재 사용자 추출 ─────────────────────
async def get_current_user(authorization: str = Header(None)) -> dict:
    """Bearer 토큰에서 사용자 정보 추출. Depends()로 사용."""
    if not JWT_AVAILABLE:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail='JWT not available')
    if not authorization or not authorization.startswith('Bearer '):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail='Authorization header missing')
    token = authorization[7:]
    payload = verify_token(token)
    if not payload:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail='Invalid token')
    return {
        'user_id': payload.get('sub'),
        'email': payload.get('email', ''),
        'is_admin': payload.get('is_admin', False),
        'tenant_id': payload.get('tenant_id'),
    }
