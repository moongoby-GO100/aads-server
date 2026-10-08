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
import re

from fastapi import Depends, HTTPException, Request

from app.auth import get_current_user

logger = logging.getLogger(__name__)

#: 기본 허용 테넌트 — 기존 열정국밥 자료의 귀속이 확인된 테넌트만.
_DEFAULT_LEGACY_TENANT_IDS = (
    "15055cac-71b0-45ec-b714-7093dde189ff",
)

_TENANT_SCOPED_PREFIXES = (
    "/api/v1/yeoljeong-finance/session",
    "/api/v1/yeoljeong-finance/tenant-registry",
    "/api/v1/yeoljeong-finance/uploads",
    "/api/v1/yeoljeong-finance/uploaded-ledger",
    "/api/v1/yeoljeong-finance/ledger-entries",
    "/api/v1/yeoljeong-finance/card-transactions",
    "/api/v1/yeoljeong-finance/card-uploads",
    "/api/v1/yeoljeong-finance/ledger-bank-transactions",
    # 전표(journals)도 같은 규칙이다 — create/list/update/transition/reverse 5개 함수
    # 전부 _tenant(user) 로 JWT tenant_id 를 잡고 _require_business() 로 사업자
    # 귀속을 확인한다. 이 줄이 없어 신규 테넌트가 전표 호출마다 403 을 받았다.
    "/api/v1/yeoljeong-finance/journals",
    # 직원 본인이 매장에 붙는 세 경로만 연다(2026-10-01 직원 가입요청 403 실측: 신규 가입자는
    # 반드시 자기 새 테넌트를 받아 레거시 허용목록에 없다).  /employees 전체를 열지 않는다 —
    # /employees/invites(목록·생성)·/employees/approved* 는 관리자 전용이라 종전 판정을 그대로 탄다.
    # 서비스가 가입요청 레코드를 고용주 테넌트(사업자 매핑)에 귀속하고, 목록은 본인 이메일 레코드만,
    # 승인·반려(PATCH /join-requests/{id}, 같은 prefix 에 걸린다)는 레코드 테넌트 == 호출자 테넌트일 때만 허용한다.
    "/api/v1/yeoljeong-finance/employees/join-requests",
    "/api/v1/yeoljeong-finance/employees/invites/resolve",
    "/api/v1/yeoljeong-finance/employees/invites/accept",
)

# 계약서 서명 두 라우트만 연다 — 조회 GET /contracts/signing/{token}, 서명 POST
# /contracts/signing.  get_contract_by_token·sign_contract 는 _read_hr(JWT 테넌트
# SQL 스코프)로만 토큰을 찾고 _contract_signer_email → _require_hr_record 로
# 레코드 테넌트를 다시 대조한다.  /contracts 전체는 열지 않는다(2026-09-30 직원
# 서명 403).
#
# 위 prefix 목록과 달리 prefix 로 열지 않는다 — (메서드, 경로 전체) 명시적 화이트리스트다.
# 경로는 fullmatch 정규식이고, GET 토큰은 서비스가 만드는 모양 그대로
# (request_contract_signature: secrets.token_urlsafe(24) → URL-safe base64 32자)만 받는다.
# 그래서 /contracts/signing-extra, /contracts/signing/{token}/sub,
# /contracts/signing/{token}-extra, /contracts/signing/signed-pdf(GET /contracts/{id}/signed-pdf
# 에 id="signing") 같은 유사 경로는 이 게이트로 열리지 않고 아래 기존 판정(레거시 테넌트
# 허용목록)을 그대로 탄다 — 게이트가 새로 403 을 만들지도, 새로 열지도 않는다.
# 기존 prefix 들의 startswith 판정은 그대로 둔다(동작 변경 없음).
_SIGNING_PATH = "/api/v1/yeoljeong-finance/contracts/signing"
_CONTRACTS_PATH = "/api/v1/yeoljeong-finance/contracts"
_CONTRACT_ID_PATTERN = r"[A-Za-z0-9_-]{8,64}"
_SIGN_TOKEN_PATTERN = r"[A-Za-z0-9_-]{32}"
_CONTRACT_SIGNING_WHITELIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(re.escape(_SIGNING_PATH) + "/" + _SIGN_TOKEN_PATTERN)),
    ("POST", re.compile(re.escape(_SIGNING_PATH))),
    # 로그인 직후 '서명할 계약서' 안내용 세 경로 — 모두 _read_hr(JWT 테넌트 SQL 스코프)와 본인 이메일 대조만 쓴다.
    ("GET", re.compile(re.escape(_CONTRACTS_PATH) + "/pending-signature")),
    ("POST", re.compile(re.escape(_CONTRACTS_PATH) + "/signing-renewal")),
    ("GET", re.compile(re.escape(_CONTRACTS_PATH) + "/" + _CONTRACT_ID_PATTERN + "/signing-view")),
)


def _is_contract_signing_route(method: str, path: str) -> bool:
    method = str(method or "").upper()
    path = str(path or "")
    return any(
        method == allowed_method and pattern.fullmatch(path) is not None
        for allowed_method, pattern in _CONTRACT_SIGNING_WHITELIST
    )


def _is_tenant_scoped_path(path: str, method: str = "GET") -> bool:
    if any(path.startswith(prefix) for prefix in _TENANT_SCOPED_PREFIXES):
        return True
    return _is_contract_signing_route(method, path)


def _allowed_tenant_ids() -> frozenset[str]:
    raw = os.getenv("OBYS_LEGACY_TENANT_IDS", "")
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    return frozenset(ids) if ids else frozenset(_DEFAULT_LEGACY_TENANT_IDS)


def is_legacy_obys_tenant(user: dict) -> bool:
    """Return whether the authenticated tenant owns the legacy OBYS ledgers."""
    tenant_id = str((user or {}).get("tenant_id") or "").strip()
    return bool(tenant_id and tenant_id in _allowed_tenant_ids())


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
    if tenant_id and _is_tenant_scoped_path(request.url.path, getattr(request, "method", "")):
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
