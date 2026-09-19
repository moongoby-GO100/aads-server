"""오비서(yeoljeong) 레거시 API 긴급 테넌트 게이트.

OBYS-SEC-TENANT-GATE-20260919-R2: obys DB 106개 API 중 테넌트 스코프가
있는 것은 obys_inventory.py 14개뿐이었다. 나머지 92개(finance/accounting/
ops/dashboard)는 로그인 여부만 확인하고 WHERE 절에 테넌트 조건이 없어
타 사업자 데이터가 그대로 노출됐다(신규 가입 JWT 재현: settings 4곳,
sales 3,812행 등). 데이터 모델을 스코프별로 고치는 작업이 끝나기 전까지,
허용 목록에 없는 tenant_id는 라우터 단위로 즉시 403 차단한다.
"""
from __future__ import annotations

import logging
import os

from fastapi import Depends, HTTPException, Request

from app.auth import get_current_user

logger = logging.getLogger(__name__)

#: 기본 허용 테넌트 — 기존 열정국밥 자료의 귀속이 확인된 테넌트만.
_DEFAULT_LEGACY_TENANT_IDS = (
    "15055cac-71b0-45ec-b714-7093dde189ff",
)

_TENANT_SCOPED_PREFIXES = (
    "/api/v1/yeoljeong-finance/tenant-registry",
    "/api/v1/yeoljeong-finance/uploads",
    "/api/v1/yeoljeong-finance/uploaded-ledger",
)


def _allowed_tenant_ids() -> frozenset[str]:
    raw = os.getenv("OBYS_LEGACY_TENANT_IDS", "")
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    return frozenset(ids) if ids else frozenset(_DEFAULT_LEGACY_TENANT_IDS)


async def require_legacy_obys_access(
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    """오비서 레거시(테넌트 미스코프) 라우터용 의존성.

    tenant_id 기준으로만 판정한다 — is_admin 플래그는 보지 않는다.
    """
    tenant_id = str((user or {}).get("tenant_id") or "").strip()
    if tenant_id and tenant_id in _allowed_tenant_ids():
        return user

    # These routes bind every query to the JWT tenant.  Role flags deliberately
    # do not affect this decision, so owner/admin cannot cross the boundary.
    if tenant_id and any(request.url.path.startswith(prefix) for prefix in _TENANT_SCOPED_PREFIXES):
        return user

    logger.warning(
        "obys_tenant_gate: blocked tenant_id=%r path=%r",
        tenant_id,
        request.url.path,
    )
    raise HTTPException(
        status_code=403,
        detail="이 계정에는 오비서 레거시 데이터 접근 권한이 없습니다. 멀티테넌트 격리 작업이 진행 중입니다.",
    )
