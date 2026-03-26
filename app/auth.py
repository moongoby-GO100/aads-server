import os
import time
import hmac
import logging
from typing import Optional

import structlog

log = structlog.get_logger()

try:
    import jwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    log.warning('pyjwt_not_installed', detail='auth endpoints will return 503')

try:
    from passlib.hash import bcrypt as passlib_bcrypt
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False
    log.warning('passlib_not_installed', detail='SaaS registration will be unavailable')

SECRET_KEY = os.getenv('JWT_SECRET_KEY', '')
ALGORITHM = 'HS256'
TOKEN_EXPIRE_HOURS = 24

ADMIN_EMAIL = os.getenv('AADS_ADMIN_EMAIL', 'admin@aads.dev')
ADMIN_PASSWORD = os.getenv('AADS_ADMIN_PASSWORD', '')

if not SECRET_KEY:
    raise RuntimeError(
        'JWT_SECRET_KEY environment variable is not set. '
        'Set it in .env before starting the server.'
    )

if not ADMIN_PASSWORD:
    log.warning('admin_password_not_set', detail='Auth endpoints will return 503 until AADS_ADMIN_PASSWORD is set')


def create_token(user_id: str, email: str, *, is_admin: bool = False) -> str:
    if not JWT_AVAILABLE:
        raise RuntimeError('PyJWT not installed')
    payload = {
        'sub': user_id,
        'email': email,
        'is_admin': is_admin,
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
            CREATE TABLE IF NOT EXISTS saas_users (
                id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)


async def create_saas_user(email: str, password: str, name: Optional[str] = None) -> Optional[dict]:
    if not BCRYPT_AVAILABLE:
        log.error('bcrypt_unavailable', detail='passlib[bcrypt] not installed')
        return None
    try:
        password_hash = passlib_bcrypt.hash(password)
        pool = await _ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO saas_users (email, password_hash, name)
                   VALUES ($1, $2, $3)
                   RETURNING id, email, name, created_at""",
                email, password_hash, name
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
                'SELECT id, email, name, password_hash FROM saas_users WHERE email = $1',
                email
            )
            if not row:
                return None
            if passlib_bcrypt.verify(password, row['password_hash']):
                return {'id': row['id'], 'email': row['email'], 'name': row['name']}
            return None
    except Exception as e:
        log.error('authenticate_saas_user_failed', error=str(e))
        return None


async def get_saas_user_by_email(email: str) -> Optional[dict]:
    try:
        pool = await _ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, email, name FROM saas_users WHERE email = $1',
                email
            )
            return dict(row) if row else None
    except Exception as e:
        log.error('get_saas_user_failed', error=str(e))
        return None
