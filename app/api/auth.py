"""JWT 인증 API 라우터 — SaaS 회원가입 + 로그인"""
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from typing import Optional
import logging

import app.auth as auth_module
from app.services import obys_account_recovery as recovery
from app.auth import TenantRole, get_current_user, require_tenant_role
from app.services.tenant_usage_limits import get_tenant_usage_summary

router = APIRouter()
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    email: str
    password: str


class OnboardingInviteRequest(BaseModel):
    email: str
    role: str = "member"

    @field_validator("email")
    @classmethod
    def onboarding_invite_email_format(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("유효하지 않은 이메일 형식입니다")
        return v


REQUIRED_CONSENT_KEYS = {"terms", "privacy", "age14"}


class ConsentItem(BaseModel):
    consent_key: str
    version: str
    agreed: bool


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    organization_name: Optional[str] = None
    team_invites: list[OnboardingInviteRequest] = Field(default_factory=list)
    consents: list[ConsentItem] = Field(default_factory=list)

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
    onboarding_required: bool = False
    tenant: Optional[dict] = None
    invites: list[dict] = Field(default_factory=list)
    must_change_password: bool = False


class TenantCreateRequest(BaseModel):
    name: str
    slug: Optional[str] = None
    plan_key: str = "free"

    @field_validator("name")
    @classmethod
    def name_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("조직명은 필수입니다")
        return v


class TenantInviteRequest(BaseModel):
    email: str
    role: str = "member"
    expires_in_hours: int = 24 * 7

    @field_validator("email")
    @classmethod
    def invite_email_format(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("유효하지 않은 이메일 형식입니다")
        return v


class TenantInviteAcceptRequest(BaseModel):
    token: str
    password: str
    name: Optional[str] = None

    @field_validator("password")
    @classmethod
    def invite_password_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("비밀번호는 최소 6자 이상이어야 합니다")
        return v


class TenantPlanUpdateRequest(BaseModel):
    plan_key: str


class TenantOnboardingRequest(BaseModel):
    organization_name: str
    team_invites: list[OnboardingInviteRequest] = Field(default_factory=list)

    @field_validator("organization_name")
    @classmethod
    def organization_name_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("조직명은 필수입니다")
        return v


def _user_id(context: dict) -> str:
    return str(context["user"]["user_id"])


def _tenant_id(context: dict) -> str:
    return str(context["tenant"]["id"])


def _assert_path_tenant(context: dict, tenant_id: str) -> None:
    if _tenant_id(context) != str(tenant_id):
        raise HTTPException(status_code=403, detail="Tenant access denied")


@router.post("/auth/register", response_model=AuthResponse)
async def register(req: RegisterRequest, request: Request):
    """SaaS 회원가입"""
    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 인증 모듈 미설치 (pip install PyJWT)")

    if req.consents:
        # consents 를 보냈다면 필수 3종(terms/privacy/age14)이 모두 agreed=true 여야 한다.
        # 일부만 보내거나 false 로 보내면 거절한다. 아예 안 보내면 기존 클라이언트 호환을 위해 통과시킨다.
        agreed_keys = {c.consent_key for c in req.consents if c.agreed}
        missing = sorted(REQUIRED_CONSENT_KEYS - agreed_keys)
        if missing:
            raise HTTPException(status_code=400, detail=f"필수 동의 항목입니다: {', '.join(missing)}")

    # 승인자가 사람을 알아볼 수 있어야 한다 — 빈 이름이나 이메일 로컬파트를 이름으로 저장하지 않는다.
    name = (req.name or "").strip()
    if not name or name.casefold() == req.email.split("@")[0].strip().casefold():
        raise HTTPException(status_code=400, detail="실명을 입력해 주십시오")

    await auth_module.require_saas_schema_ready()

    existing = await auth_module.get_saas_user_by_email(req.email)
    if existing:
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다")

    user = await auth_module.create_saas_user(
        req.email,
        req.password,
        name,
        attach_internal_tenant=False,
        consents=[c.model_dump() for c in req.consents] or None,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    if not user:
        raise HTTPException(status_code=500, detail="회원가입 처리 중 오류가 발생했습니다")

    uid = str(user["id"])  # DB returns int, JWT/response need str
    tenant = await auth_module.create_tenant_for_user(
        user_id=uid,
        name=req.organization_name or f"{name} Workspace",
        plan_key="free",
    )
    tenant_id = str(tenant.get("tenant_id") or "") or None
    invites = []
    for invite in req.team_invites:
        created_invite = await auth_module.create_tenant_invite(
            tenant_id=str(tenant_id),
            email=invite.email,
            role=invite.role,
            invited_by=uid,
        )
        invites.append(created_invite)
    token = auth_module.create_token(
        uid, user["email"], tenant_id=tenant_id, session_version=await auth_module.session_version_for(uid)
    )
    logger.info("SaaS 회원가입 완료: %s", req.email)
    return AuthResponse(
        token=token,
        user_id=uid,
        email=user["email"],
        name=user.get("name"),
        is_admin=False,
        tenant_id=tenant_id,
        onboarding_required=not bool(req.organization_name),
        tenant=tenant,
        invites=invites,
    )


@router.post("/auth/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    """1순위: SaaS DB 인증, 2순위: 관리자가 발급한 임시 비밀번호, 3순위: CEO 관리자 환경변수"""
    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 인증 모듈 미설치 (pip install PyJWT)")

    await auth_module.require_saas_schema_ready()
    saas_user = await auth_module.authenticate_saas_user(req.email, req.password)
    must_change = False
    if not saas_user:
        saas_user = await _try_temp_password_login(req.email, req.password)
        must_change = bool(saas_user)
    if saas_user:
        uid = str(saas_user["id"])  # DB returns int, JWT/response need str
        tenant_id = await auth_module.resolve_login_tenant_for_user(saas_user)
        await auth_module.update_saas_user_last_login(uid)
        token = auth_module.create_token(
            uid,
            saas_user["email"],
            tenant_id=tenant_id,
            session_version=await auth_module.session_version_for(uid),
            expires_in_seconds=_FORCED_CHANGE_TOKEN_SECONDS if must_change else None,
        )
        return AuthResponse(
            token=token,
            user_id=uid,
            email=saas_user["email"],
            name=saas_user.get("name"),
            is_admin=False,
            tenant_id=tenant_id,
            must_change_password=must_change,
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


_FORCED_CHANGE_TOKEN_SECONDS = 3600


async def _try_temp_password_login(email: str, password: str) -> Optional[dict]:
    """복구 테이블이 없는 DB(AADS 본 앱 등)·조회 오류는 '임시 비밀번호 아님'으로 취급해 일반 로그인 실패 경로를 유지한다."""
    try:
        store = recovery.get_store()
        if not await store.schema_ready():
            return None
        return await recovery.try_temp_password_login(store, email, password)
    except Exception as exc:  # noqa: BLE001
        logger.warning("temp password login check failed: %s", type(exc).__name__)
        return None


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "")


async def _recovery_store() -> "recovery.RecoveryStore":
    store = recovery.get_store()
    if not await store.schema_ready():
        raise HTTPException(status_code=503, detail="계정 복구 기능을 아직 사용할 수 없습니다. 관리자에게 문의해 주십시오.")
    return store


def _recovery_http(exc: "recovery.RecoveryError") -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


class FindIdRequest(BaseModel):
    name: str = Field(max_length=100)
    phone: str = Field(max_length=40)


class PasswordResetRequest(BaseModel):
    email: str = Field(max_length=200)
    name: str = Field(max_length=100)
    phone: str = Field(max_length=40)


class SmsConfirmRequest(BaseModel):
    email: str = Field(max_length=200)
    code: str = Field(max_length=12)
    new_password: str = Field(max_length=200)


class PasswordChangeRequest(BaseModel):
    new_password: str = Field(max_length=200)
    current_password: Optional[str] = Field(default=None, max_length=200)


@router.get("/auth/recovery/options")
async def recovery_options():
    return recovery.options()


@router.post("/auth/recovery/find-id")
async def recovery_find_id(req: FindIdRequest, request: Request):
    store = await _recovery_store()
    try:
        return await recovery.find_id(store, req.name, req.phone, _client_ip(request))
    except recovery.RecoveryError as exc:
        raise _recovery_http(exc)


@router.post("/auth/recovery/password-reset-request")
async def recovery_password_reset_request(req: PasswordResetRequest, request: Request):
    store = await _recovery_store()
    try:
        return await recovery.request_password_reset(store, req.email, req.name, req.phone, _client_ip(request))
    except recovery.RecoveryError as exc:
        raise _recovery_http(exc)


@router.post("/auth/recovery/sms/request")
async def recovery_sms_request(req: PasswordResetRequest, request: Request):
    store = await _recovery_store()
    try:
        return await recovery.request_sms_code(store, req.email, req.name, req.phone, _client_ip(request))
    except recovery.RecoveryError as exc:
        raise _recovery_http(exc)


@router.post("/auth/recovery/sms/confirm")
async def recovery_sms_confirm(req: SmsConfirmRequest, request: Request):
    store = await _recovery_store()
    try:
        result = await recovery.confirm_sms_reset(store, req.email, req.code, req.new_password, _client_ip(request))
    except recovery.RecoveryError as exc:
        raise _recovery_http(exc)
    result.pop("session_version", None)
    return result


@router.post("/auth/password/change")
async def change_my_password(req: PasswordChangeRequest, current_user: dict = Depends(get_current_user)):
    """본인 비밀번호 변경. 임시 비밀번호로 로그인한 세션(must_change_password)은 현재 비밀번호 없이 변경한다."""
    if current_user.get("is_admin") or str(current_user.get("user_id")) == "admin":
        raise HTTPException(status_code=403, detail="환경변수 관리자 계정은 이 경로로 변경할 수 없습니다.")
    store = await _recovery_store()
    uid = str(current_user["user_id"])
    try:
        result = await recovery.change_password(
            store,
            user_id=uid,
            new_password=req.new_password,
            current_password=req.current_password,
            must_change=bool(current_user.get("must_change_password")),
        )
    except recovery.RecoveryError as exc:
        raise _recovery_http(exc)
    token = auth_module.create_token(
        uid,
        str(current_user.get("email") or ""),
        tenant_id=current_user.get("tenant_id"),
        session_version=int(result.get("session_version") or 0),
    )
    return {"changed": True, "token": token, "message": "비밀번호를 변경했습니다."}


@router.get("/auth/password-reset-requests")
async def list_password_reset_requests(context: dict = Depends(require_tenant_role(TenantRole.ADMIN))):
    store = await _recovery_store()
    return {"requests": await recovery.list_reset_requests(store, _tenant_id(context))}


@router.post("/auth/password-reset-requests/{request_id}/issue")
async def issue_password_reset(request_id: str, context: dict = Depends(require_tenant_role(TenantRole.ADMIN))):
    store = await _recovery_store()
    try:
        return await recovery.issue_temp_password(
            store,
            request_id=request_id,
            actor_id=_user_id(context),
            actor_role=str(context["membership"]["role"]),
            actor_tenant_id=_tenant_id(context),
            actor_is_internal_admin=bool(context["user"].get("is_internal_admin")),
        )
    except recovery.RecoveryError as exc:
        raise _recovery_http(exc)


@router.post("/auth/password-reset-requests/{request_id}/cancel")
async def cancel_password_reset(request_id: str, context: dict = Depends(require_tenant_role(TenantRole.ADMIN))):
    store = await _recovery_store()
    try:
        return await recovery.cancel_reset_request(
            store, request_id=request_id, actor_id=_user_id(context), actor_tenant_id=_tenant_id(context)
        )
    except recovery.RecoveryError as exc:
        raise _recovery_http(exc)


@router.post("/auth/admin/impersonate/{user_id}")
async def admin_impersonate(
    user_id: str,
    current_user: dict = Depends(auth_module.require_internal_admin),
):
    """관리자 대리 로그인 — 대상 사용자의 JWT 토큰을 발급한다."""
    import traceback as _tb
    from app.core.db_pool import get_pool
    pool = get_pool()
    if not user_id or not user_id.strip():
        raise HTTPException(status_code=400, detail="유효하지 않은 사용자 ID입니다")
    try:
        logger.info("impersonate: step1 user_id=%s type=%s", user_id, type(user_id).__name__)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, email, name, default_tenant_id::text, role FROM saas_users WHERE id = $1::text",
                str(user_id),
            )
        if not row:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        target = dict(row)
        logger.info("impersonate: step2 target_keys=%s", {k: type(v).__name__ for k, v in target.items()})
        tenant_id = await auth_module.resolve_login_tenant_for_user(target)
        logger.info("impersonate: step3 tenant_id=%s", tenant_id)
        token = auth_module.create_token(
            str(target["id"]),
            target["email"],
            tenant_id=str(tenant_id) if tenant_id else None,
            session_version=await auth_module.session_version_for(str(target["id"])),
        )
        return {
            "token": token,
            "user_id": str(target["id"]),
            "email": target["email"],
            "name": target.get("name"),
            "tenant_id": str(tenant_id) if tenant_id else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("impersonate: FAILED %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=f"Impersonate error: {type(e).__name__}: {e}")


@router.get("/auth/tenants")
async def list_my_tenants(context: dict = Depends(require_tenant_role(TenantRole.VIEWER))):
    """현재 사용자가 접근 가능한 SaaS 조직 목록."""
    tenants = await auth_module.list_user_tenants(_user_id(context))
    return {
        "current_tenant_id": _tenant_id(context),
        "tenants": tenants,
    }


@router.post("/auth/tenants", status_code=201)
async def create_tenant(
    req: TenantCreateRequest,
    context: dict = Depends(require_tenant_role(TenantRole.VIEWER)),
):
    """셀프서비스 조직 생성 — 생성자는 owner가 되고 기본 tenant가 전환된다."""
    tenant = await auth_module.create_tenant_for_user(
        user_id=_user_id(context),
        name=req.name,
        slug=req.slug,
        plan_key=req.plan_key,
    )
    user = context["user"]
    token = auth_module.create_token(
        str(user["user_id"]), str(user.get("email") or ""), tenant_id=tenant["tenant_id"],
        session_version=int(user.get("session_version") or 0),
    )
    return {
        "tenant": tenant,
        "token": token,
    }


@router.post("/auth/onboarding", status_code=201)
async def complete_onboarding(
    req: TenantOnboardingRequest,
    context: dict = Depends(require_tenant_role(TenantRole.VIEWER)),
):
    """가입 직후 조직명과 팀원 초대 역할을 확정한다."""
    tenant = await auth_module.finalize_customer_tenant_onboarding(
        user_id=_user_id(context),
        tenant_id=_tenant_id(context),
        name=req.organization_name,
    )
    tenant_id = str(tenant["tenant_id"])
    invites = []
    for invite in req.team_invites:
        invites.append(
            await auth_module.create_tenant_invite(
                tenant_id=tenant_id,
                email=invite.email,
                role=invite.role,
                invited_by=_user_id(context),
            )
        )
    user = context["user"]
    token = auth_module.create_token(
        str(user["user_id"]), str(user.get("email") or ""), tenant_id=tenant_id,
        session_version=int(user.get("session_version") or 0),
    )
    return {
        "tenant": tenant,
        "invites": invites,
        "token": token,
    }


@router.post("/auth/tenants/{tenant_id}/switch")
async def switch_tenant(
    tenant_id: str,
    context: dict = Depends(require_tenant_role(TenantRole.VIEWER)),
):
    """소속 조직으로 현재 세션 tenant를 전환하는 JWT를 재발급한다."""
    switched = await auth_module.switch_user_tenant(_user_id(context), tenant_id)
    user = switched["user"]
    token = auth_module.create_token(
        str(user["id"]), str(user["email"]), tenant_id=tenant_id,
        session_version=await auth_module.session_version_for(str(user["id"])),
    )
    return {
        "token": token,
        "tenant": switched["context"]["tenant"],
        "membership": switched["context"]["membership"],
    }


@router.post("/auth/tenants/{tenant_id}/invites", status_code=201)
async def create_tenant_invite(
    tenant_id: str,
    req: TenantInviteRequest,
    context: dict = Depends(require_tenant_role(TenantRole.ADMIN)),
):
    """조직 관리자 초대 생성. token은 생성 응답에서만 노출된다."""
    _assert_path_tenant(context, tenant_id)
    return await auth_module.create_tenant_invite(
        tenant_id=tenant_id,
        email=req.email,
        role=req.role,
        invited_by=_user_id(context),
        expires_in_hours=req.expires_in_hours,
    )


@router.get("/auth/tenants/{tenant_id}/members")
async def list_tenant_members(
    tenant_id: str,
    context: dict = Depends(require_tenant_role(TenantRole.VIEWER)),
):
    """조직 팀원 목록 조회."""
    _assert_path_tenant(context, tenant_id)
    return {
        "tenant_id": tenant_id,
        "members": await auth_module.list_tenant_members(tenant_id),
    }


@router.get("/auth/tenants/{tenant_id}/invites")
async def list_tenant_invites(
    tenant_id: str,
    context: dict = Depends(require_tenant_role(TenantRole.ADMIN)),
):
    """조직 관리자용 pending 초대 목록 조회. 초대 token은 재노출하지 않는다."""
    _assert_path_tenant(context, tenant_id)
    return {
        "tenant_id": tenant_id,
        "invites": await auth_module.list_tenant_pending_invites(tenant_id),
    }


@router.post("/auth/invites/accept", response_model=AuthResponse)
async def accept_tenant_invite(req: TenantInviteAcceptRequest):
    """초대 토큰 수락. 신규 사용자는 생성하고, 기존 사용자는 비밀번호로 본인 확인한다."""
    accepted = await auth_module.accept_tenant_invite(
        token=req.token,
        password=req.password,
        name=req.name,
    )
    user = accepted["user"]
    tenant_id = accepted["tenant_id"]
    token = auth_module.create_token(
        str(user["id"]), str(user["email"]), tenant_id=tenant_id,
        session_version=await auth_module.session_version_for(str(user["id"])),
    )
    return AuthResponse(
        token=token,
        user_id=str(user["id"]),
        email=str(user["email"]),
        name=user.get("name"),
        is_admin=False,
        tenant_id=tenant_id,
    )


@router.get("/auth/tenants/{tenant_id}/usage")
async def get_tenant_usage(
    tenant_id: str,
    context: dict = Depends(require_tenant_role(TenantRole.VIEWER)),
):
    """월간 사용량/플랜 한도 조회."""
    _assert_path_tenant(context, tenant_id)
    return await get_tenant_usage_summary(tenant_id)


@router.patch("/auth/tenants/{tenant_id}/plan")
async def update_tenant_plan(
    tenant_id: str,
    req: TenantPlanUpdateRequest,
    context: dict = Depends(require_tenant_role(TenantRole.ADMIN)),
):
    """조직 플랜 정책 변경. 외부 결제 연동 없이 내부 plan_key만 갱신한다."""
    _assert_path_tenant(context, tenant_id)
    tenant = await auth_module.update_tenant_plan(tenant_id, req.plan_key, _user_id(context))
    return {"tenant": tenant}


@router.get("/auth/login/e2e-inject", response_class=HTMLResponse)
async def e2e_inject(
    credential_id: str = Query(..., description="Vault credential ID"),
    tenant_id: str = Query(..., description="Tenant ID that owns the vault credential"),
    redirect: str = Query("/chat", description="인증 후 리다이렉트 경로"),
):
    """E2E 자동 인증 — vault 자격증명으로 로그인 후 토큰을 브라우저에 주입."""
    from app.core.credential_vault import VaultUnavailableError, get_credential
    try:
        cred = await get_credential(credential_id, include_secrets=True, tenant_id=tenant_id)
        if not cred:
            raise HTTPException(status_code=404, detail="자격증명 없음")
    except HTTPException:
        raise
    except VaultUnavailableError:
        # 오비서에 OBYS_VAULT_KEY 가 없을 때. 앱은 살아 있고 vault 만 꺼진 상태다.
        raise HTTPException(status_code=503, detail="자격증명 보관소 비활성")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"자격증명 조회 실패: {e}")

    email = cred.get("username", "")
    password = cred.get("password", "")

    if not auth_module.JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT 미설치")

    await auth_module.require_saas_schema_ready()
    token = None
    saas_user = await auth_module.authenticate_saas_user(email, password)
    if saas_user:
        uid = str(saas_user["id"])
        token = auth_module.create_token(
            uid,
            saas_user["email"],
            tenant_id=await auth_module.resolve_login_tenant_for_user(saas_user),
            session_version=await auth_module.session_version_for(uid),
        )
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
    await mark_used(credential_id, tenant_id=tenant_id)
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
async def get_me(current_user: dict = Depends(get_current_user)):
    """현재 로그인 사용자 정보"""
    return {
        "user_id": current_user.get("user_id"),
        "email": current_user.get("email"),
        "is_admin": current_user.get("is_admin", False),
        "is_internal_admin": current_user.get("is_internal_admin", False),
        "tenant_id": current_user.get("tenant_id"),
        "tenant_role": current_user.get("tenant_role"),
        "user_role": current_user.get("user_role"),
        "tenant": current_user.get("current_tenant"),
        "membership": current_user.get("current_membership"),
        "must_change_password": bool(current_user.get("must_change_password", False)),
    }
