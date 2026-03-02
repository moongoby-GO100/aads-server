"""JWT 인증 모듈 — MVP 단일 관리자 계정"""
import os
import time
import logging
from typing import Optional

log = logging.getLogger(__name__)

try:
    import jwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    log.warning("PyJWT not installed — auth endpoints will return 503")

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "aads-dev-secret-key-change-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

ADMIN_EMAIL = os.getenv("AADS_ADMIN_EMAIL", "admin@aads.dev")
ADMIN_PASSWORD = os.getenv("AADS_ADMIN_PASSWORD", "aads-admin-password")


def create_token(user_id: str, email: str) -> str:
    if not JWT_AVAILABLE:
        raise RuntimeError("PyJWT not installed")
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_EXPIRE_HOURS * 3600,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    if not JWT_AVAILABLE:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except Exception as e:
        log.debug("Token verification failed: %s", e)
        return None


def check_admin_credentials(email: str, password: str) -> bool:
    return email == ADMIN_EMAIL and password == ADMIN_PASSWORD
