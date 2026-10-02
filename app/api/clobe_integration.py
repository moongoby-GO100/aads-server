"""클로브AI MCP 연결 — 관리자 전용 API. 콜백만 인증 면제(state 가 인증)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.auth import require_internal_admin
from app.services import clobe_mcp_client as clobe

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations/clobe", tags=["clobe-integration"])

_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _page(message: str, status_code: int) -> HTMLResponse:
    html = f"<!doctype html><meta charset=utf-8><title>Clobe</title><p>{message}</p>"
    return HTMLResponse(html, status_code=status_code, headers=_NO_STORE)


def _http_error(exc: clobe.ClobeError) -> HTTPException:
    if isinstance(exc, clobe.ClobeReauthRequired):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, clobe.ClobeToolDenied):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.post("/oauth/start")
async def oauth_start(admin: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    try:
        return await clobe.start_authorization(created_by=str(admin.get("user_id") or ""))
    except clobe.ClobeError as exc:
        raise _http_error(exc) from None


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(
    code: str | None = Query(None, max_length=2048),
    state: str | None = Query(None, max_length=256),
    error: str | None = Query(None, max_length=100),
) -> HTMLResponse:
    try:
        await clobe.handle_callback(code=code, state=state, error=error)
    except clobe.ClobeError as exc:
        logger.warning("clobe_callback_rejected reason=%s", str(exc).split(":")[0])
        return _page("연결에 실패했습니다. 관리자에게 문의하세요.", 400)
    except Exception as exc:  # noqa: BLE001 - 예외 메시지에 응답 본문이 섞일 수 있어 유형만 남긴다
        logger.error("clobe_callback_error type=%s", type(exc).__name__)
        return _page("연결 처리 중 오류가 발생했습니다.", 500)
    return _page("클로브AI 연결이 완료되었습니다. 이 창을 닫아도 됩니다.", 200)


@router.get("/status")
async def status(_: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    return await clobe.get_status()


@router.post("/tools/refresh")
async def tools_refresh(_: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    try:
        return await clobe.list_tools()
    except clobe.ClobeError as exc:
        raise _http_error(exc) from None


@router.post("/verify")
async def verify(_: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    try:
        return await clobe.verify_connection()
    except clobe.ClobeError as exc:
        raise _http_error(exc) from None


@router.post("/revoke")
async def revoke(_: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    return await clobe.revoke_connection()
