"""E2E Credential Vault API — 자격증명 CRUD + 로그인 테스트 엔드포인트."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.credential_vault import (
    create_credential,
    delete_credential,
    execute_login_steps,
    get_credential,
    get_login_credential,
    list_credentials,
    mark_verified,
    update_credential,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/e2e/credentials", tags=["e2e-credentials"])


# ── Request/Response 모델 ──────────────────────────────

class CredentialCreate(BaseModel):
    """자격증명 생성 요청."""
    service: str = Field(..., description="서비스명 (aads-dashboard, newtalk-admin 등)")
    username: str = Field(..., description="로그인 아이디/이메일")
    password: str = Field(..., description="로그인 비밀번호")
    project: str | None = Field(None, description="프로젝트 (AADS/KIS/GO100/SF/NTV2/NAS)")
    label: str = Field("기본", description="라벨 (관리자/CEO계정/테스트계정)")
    login_url: str | None = Field(None, description="로그인 페이지 URL")
    extra_fields: dict[str, str] | None = Field(None, description="추가 필드 (OTP, API key 등)")
    login_steps: list[dict[str, Any]] | None = Field(None, description="Playwright 자동화 스텝")


class CredentialUpdate(BaseModel):
    """자격증명 수정 요청."""
    service: str | None = None
    username: str | None = None
    password: str | None = None
    project: str | None = None
    label: str | None = None
    login_url: str | None = None
    extra_fields: dict[str, str] | None = None
    login_steps: list[dict[str, Any]] | None = None
    is_active: bool | None = None


class LoginTestRequest(BaseModel):
    """로그인 테스트 요청."""
    service: str
    project: str | None = None
    label: str = "기본"


# ── 엔드포인트 ─────────────────────────────────────────

@router.get("")
async def api_list_credentials(
    project: str | None = None,
    service: str | None = None,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """자격증명 목록 조회. include_secrets=true로 비밀번호 포함."""
    items = await list_credentials(project=project, service=service, include_secrets=include_secrets)
    return {"credentials": items, "count": len(items)}


@router.get("/{credential_id}")
async def api_get_credential(
    credential_id: str,
    include_secrets: bool = True,
) -> dict[str, Any]:
    """단일 자격증명 상세 조회."""
    item = await get_credential(credential_id, include_secrets=include_secrets)
    if not item:
        raise HTTPException(status_code=404, detail="자격증명을 찾을 수 없습니다")
    return item


@router.post("")
async def api_create_credential(body: CredentialCreate) -> dict[str, Any]:
    """새 자격증명 등록 (암호화 저장)."""
    result = await create_credential(
        service=body.service,
        username=body.username,
        password=body.password,
        project=body.project,
        label=body.label,
        login_url=body.login_url,
        extra_fields=body.extra_fields,
        login_steps=body.login_steps,
    )
    return {"status": "created", "credential": result}


@router.put("/{credential_id}")
async def api_update_credential(credential_id: str, body: CredentialUpdate) -> dict[str, Any]:
    """자격증명 수정."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="수정할 필드가 없습니다")
    result = await update_credential(credential_id, **updates)
    if not result:
        raise HTTPException(status_code=404, detail="자격증명을 찾을 수 없습니다")
    return {"status": "updated", "credential": result}


@router.delete("/{credential_id}")
async def api_delete_credential(credential_id: str) -> dict[str, Any]:
    """자격증명 비활성화 (소프트 삭제)."""
    success = await delete_credential(credential_id)
    if not success:
        raise HTTPException(status_code=404, detail="자격증명을 찾을 수 없습니다")
    return {"status": "deleted", "credential_id": credential_id}


@router.post("/test-login")
async def api_test_login(body: LoginTestRequest) -> dict[str, Any]:
    """저장된 자격증명으로 실제 로그인 테스트 실행."""
    cred = await get_login_credential(
        service=body.service,
        project=body.project,
        label=body.label,
    )
    if not cred:
        raise HTTPException(
            status_code=404,
            detail=f"자격증명 없음: service={body.service}, project={body.project}, label={body.label}",
        )

    login_url = cred.get("login_url")
    if not login_url:
        raise HTTPException(status_code=400, detail="login_url이 설정되지 않았습니다")

    # Playwright 브라우저로 실제 로그인 테스트
    try:
        from app.api.ceo_chat_tools import _get_or_create_browser_context
        browser_ctx = await _get_or_create_browser_context()
        page = await browser_ctx.new_page()

        try:
            if not cred.get("login_steps"):
                await page.goto(login_url, wait_until="domcontentloaded", timeout=15000)
            success = await execute_login_steps(page, cred)
            final_url = page.url

            if success:
                await mark_verified(cred["id"], success=True)
                return {
                    "status": "success",
                    "service": body.service,
                    "final_url": final_url,
                    "message": "로그인 성공",
                }
            else:
                return {
                    "status": "failed",
                    "service": body.service,
                    "final_url": final_url,
                    "message": "로그인 스텝 실행 중 오류 발생",
                }
        finally:
            await page.close()

    except Exception as e:
        logger.error("로그인 테스트 실패: %s", e)
        return {
            "status": "error",
            "service": body.service,
            "message": str(e),
        }
