"""JWT 인증 API 라우터"""
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import app.auth as auth_module

router = APIRouter()


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user_id: str
    email: str


@router.post("/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 인증 모듈 미설치 (pip install PyJWT)")
    if not auth_module.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="관리자 비밀번호 미설정 (AADS_ADMIN_PASSWORD env var 필요)")
    if not auth_module.check_admin_credentials(req.email, req.password):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다")
    token = auth_module.create_token("1", req.email)
    return LoginResponse(token=token, user_id="1", email=req.email)


@router.get("/auth/me")
async def get_me(authorization: Optional[str] = Header(None)):
    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 인증 모듈 미설치")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization 헤더가 없습니다")
    token = authorization[7:]
    payload = auth_module.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰")
    return {"user_id": payload.get("sub"), "email": payload.get("email")}
