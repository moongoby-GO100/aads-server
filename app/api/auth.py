"""JWT 인증 API 라우터 — SaaS 회원가입 + 로그인"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, field_validator
from typing import Optional
import logging

import app.auth as auth_module

router = APIRouter()
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None

    @field_validator("email")
    @classmethod
    def email_format(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("유효하지 않은 이메일 형식입니다")
        return v

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("비밀번호는 최소 6자 이상이어야 합니다")
        return v


class AuthResponse(BaseModel):
    token: str
    user_id: str
    email: str
    name: Optional[str] = None
    is_admin: bool = False


@router.post("/auth/register", response_model=AuthResponse)
async def register(req: RegisterRequest):
    """SaaS 회원가입"""
    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 인증 모듈 미설치 (pip install PyJWT)")

    await auth_module.ensure_saas_users_table()

    existing = await auth_module.get_saas_user_by_email(req.email)
    if existing:
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다")

    user = await auth_module.create_saas_user(req.email, req.password, req.name)
    if not user:
        raise HTTPException(status_code=500, detail="회원가입 처리 중 오류가 발생했습니다")

    token = auth_module.create_token(user["id"], user["email"])
    logger.info("SaaS 회원가입 완료: %s", req.email)
    return AuthResponse(
        token=token,
        user_id=user["id"],
        email=user["email"],
        name=user.get("name"),
        is_admin=False,
    )


@router.post("/auth/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    """1순위: SaaS DB 인증, 2순위: CEO 관리자 환경변수"""
    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 인증 모듈 미설치 (pip install PyJWT)")

    await auth_module.ensure_saas_users_table()
    saas_user = await auth_module.authenticate_saas_user(req.email, req.password)
    if saas_user:
        token = auth_module.create_token(saas_user["id"], saas_user["email"])
        return AuthResponse(
            token=token,
            user_id=saas_user["id"],
            email=saas_user["email"],
            name=saas_user.get("name"),
            is_admin=False,
        )

    if auth_module.ADMIN_PASSWORD and auth_module.check_admin_credentials(req.email, req.password):
        token = auth_module.create_token("admin", req.email, is_admin=True)
        return AuthResponse(
            token=token,
            user_id="admin",
            email=req.email,
            is_admin=True,
        )

    raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다")


@router.get("/auth/me")
async def get_me(authorization: Optional[str] = Header(None)):
    """현재 로그인 사용자 정보"""
    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 인증 모듈 미설치")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization 헤더가 없습니다")
    token = authorization[7:]
    payload = auth_module.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰")
    return {
        "user_id": payload.get("sub"),
        "email": payload.get("email"),
        "is_admin": payload.get("is_admin", False),
    }
