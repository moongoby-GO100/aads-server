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

SECRET_KEY = os.getenv('JWT_SECRET_KEY', '')
ALGORITHM = 'HS256'
TOKEN_EXPIRE_HOURS = 24

ADMIN_EMAIL = os.getenv('AADS_ADMIN_EMAIL', 'admin@aads.dev')
ADMIN_PASSWORD = os.getenv('AADS_ADMIN_PASSWORD', '')

if not SECRET_KEY:
    log.warning('jwt_secret_key_not_set', detail='Using insecure default - set JWT_SECRET_KEY in production')
    SECRET_KEY = 'aads-insecure-default-CHANGE-ME'

if not ADMIN_PASSWORD:
    log.warning('admin_password_not_set', detail='Auth endpoints will return 503 until AADS_ADMIN_PASSWORD is set')


def create_token(user_id: str, email: str) -> str:
    if not JWT_AVAILABLE:
        raise RuntimeError('PyJWT not installed')
    payload = {
        'sub': user_id,
        'email': email,
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
