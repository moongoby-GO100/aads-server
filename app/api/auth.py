"""JWT 인증 API 라우터 — SaaS 회원가입 + 로그인"""
from fastapi import APIRouter, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
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
    tenant_id: Optional[str] = None


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

    uid = str(user["id"])  # DB returns int, JWT/response need str
    tenant_id = str(user.get("default_tenant_id") or "") or None
    token = auth_module.create_token(uid, user["email"], tenant_id=tenant_id)
    logger.info("SaaS 회원가입 완료: %s", req.email)
    return AuthResponse(
        token=token,
        user_id=uid,
        email=user["email"],
        name=user.get("name"),
        is_admin=False,
        tenant_id=tenant_id,
    )


@router.post("/auth/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    """1순위: SaaS DB 인증, 2순위: CEO 관리자 환경변수"""
    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 인증 모듈 미설치 (pip install PyJWT)")

    await auth_module.ensure_saas_users_table()
    saas_user = await auth_module.authenticate_saas_user(req.email, req.password)
    if saas_user:
        uid = str(saas_user["id"])  # DB returns int, JWT/response need str
        tenant_id = saas_user.get("tenant_id")
        token = auth_module.create_token(uid, saas_user["email"], tenant_id=tenant_id)
        return AuthResponse(
            token=token,
            user_id=uid,
            email=saas_user["email"],
            name=saas_user.get("name"),
            is_admin=False,
            tenant_id=tenant_id,
        )

    if auth_module.ADMIN_PASSWORD and auth_module.check_admin_credentials(req.email, req.password):
        tenant_id = await auth_module.get_internal_tenant_id()
        token = auth_module.create_token("admin", req.email, is_admin=True, tenant_id=tenant_id)
        return AuthResponse(
            token=token,
            user_id="admin",
            email=req.email,
            is_admin=True,
            tenant_id=tenant_id,
        )

    raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다")


@router.get("/auth/login/e2e-inject", response_class=HTMLResponse)
async def e2e_inject(
    credential_id: str = Query(..., description="Vault credential ID"),
    redirect: str = Query("/chat", description="인증 후 리다이렉트 경로"),
):
    """E2E 자동 인증 — vault 자격증명으로 로그인 후 토큰을 브라우저에 주입."""
    try:
        from app.core.credential_vault import get_credential
        cred = await get_credential(credential_id, include_secrets=True)
        if not cred:
            raise HTTPException(status_code=404, detail="자격증명 없음")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"자격증명 조회 실패: {e}")

    email = cred.get("username", "")
    password = cred.get("password", "")

    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 미설치")

    await auth_module.ensure_saas_users_table()
    token = None
    saas_user = await auth_module.authenticate_saas_user(email, password)
    if saas_user:
        uid = str(saas_user["id"])
        token = auth_module.create_token(uid, saas_user["email"], tenant_id=saas_user.get("tenant_id"))
    elif auth_module.ADMIN_PASSWORD and auth_module.check_admin_credentials(email, password):
        token = auth_module.create_token(
            "admin",
            email,
            is_admin=True,
            tenant_id=await auth_module.get_internal_tenant_id(),
        )

    if not token:
        raise HTTPException(status_code=401, detail="자격증명 인증 실패")

    from app.core.credential_vault import mark_used
    await mark_used(credential_id)
    logger.info("e2e_inject: credential_id=%s email=%s redirect=%s", credential_id, email, redirect)

    import html as html_mod
    safe_redirect = html_mod.escape(redirect)
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>E2E Auth</title></head>
<body>
<script>
localStorage.setItem('aads_token', '{token}');
document.cookie = 'aads_token={token}; path=/; max-age=604800; SameSite=Lax';
window.location.href = '{safe_redirect}';
</script>
<noscript>인증 완료 — JS가 필요합니다.</noscript>
</body></html>""")


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
        "tenant_id": payload.get("tenant_id"),
    }
