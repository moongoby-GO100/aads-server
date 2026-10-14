"""도메인으로 Vault 계정을 찾아 초안에 연결한다 (설계서 7.2).

- 계정 조회는 id·표시명·origin·검증 여부만 읽는다. 아이디/비밀번호 열은 SELECT 하지 않는다 — 복호화 경로 자체가 없다.
- 도메인 일치는 정확한 호스트가 아니라 기준 도메인(store.coupangeats.com ≈ coupangeats.com)으로 본다.
  로그인 화면이 store./login. 같은 서브도메인에 있어도 목록에서 빠지지 않게 한다.
- 일치가 없다고 해서 Vault 가 비었다고 단정하지 않는다: 'none' 은 이 도메인에 연결된 계정이 없다는 뜻이다.
- 새 계정은 기존 보안 입력 요청(`vault_secure_input`)만 쓴다. 초안에는 요청 id 와 계정 id 참조만 남는다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import urlparse

from app.core.db_pool import get_pool
from app.services import vault_secure_input
from app.services.automation_intent_parser import base_domain, normalize_host

REQUEST_TTL_MINUTES = 30
MAX_CANDIDATES = 50

_LIST_SQL = """
SELECT id, label, origin, metadata, last_used_at, updated_at
  FROM agent_vault_credentials
 WHERE tenant_id = $1::uuid AND is_active = TRUE AND lower(origin) LIKE $2
 ORDER BY last_used_at DESC NULLS LAST, label
 LIMIT $3
"""

_ONE_SQL = """
SELECT id, label, origin, metadata, last_used_at, updated_at
  FROM agent_vault_credentials
 WHERE id = $1::uuid AND tenant_id = $2::uuid AND is_active = TRUE
"""


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _host_of_origin(origin: str) -> str:
    text = str(origin or "").strip()
    return normalize_host(text if "//" in text else f"//{text}")


def domain_matches(origin: str, domain: str) -> bool:
    """Vault origin 이 자동화 도메인과 같은 사이트인가(서브도메인 차이 허용)."""
    host, target = _host_of_origin(origin), normalize_host(domain)
    return bool(host and target and base_domain(host) == base_domain(target))


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """화면에 내보내는 계정 요약. 표시명·사이트 이름·검증 여부뿐이다."""
    host = _host_of_origin(str(row.get("origin") or ""))
    meta = _metadata(row.get("metadata"))
    return {
        "id": str(row["id"]),
        "display_name": str(row.get("label") or "").strip() or host,
        "host": host,
        "verified": str(meta.get("verification_status") or "").strip().lower() == "verified",
    }


async def find_credentials(*, tenant_id: str, domain: str) -> list[dict[str, Any]]:
    """이 테넌트에서 domain 과 같은 사이트의 활성 계정 요약 목록."""
    target = normalize_host(domain)
    if not target:
        return []
    needle = f"%{base_domain(target)}%"
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(_LIST_SQL, str(tenant_id), needle, MAX_CANDIDATES)
    matched = [dict(r) for r in rows if domain_matches(str(r["origin"]), target)]
    matched.sort(key=lambda r: _host_of_origin(str(r["origin"])) != target)
    return [public_view(r) for r in matched]


async def link_status(*, tenant_id: str, domain: str) -> dict[str, Any]:
    """{'status': 'single'|'choices'|'none', 'credentials': [...]}."""
    credentials = await find_credentials(tenant_id=tenant_id, domain=domain)
    status = "none" if not credentials else ("single" if len(credentials) == 1 else "choices")
    return {"status": status, "credentials": credentials}


async def get_credential_for_domain(*, tenant_id: str, credential_id: str, domain: str) -> dict[str, Any] | None:
    """선택한 계정이 이 테넌트 것이고 자동화 도메인과 같은 사이트일 때만 요약을 돌려준다."""
    try:
        cred_uuid = str(uuid.UUID(str(credential_id)))
    except ValueError:
        return None
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(_ONE_SQL, cred_uuid, str(tenant_id))
    if row is None or not domain_matches(str(row["origin"]), domain):
        return None
    return public_view(dict(row))


async def request_new_input(
    *, tenant_id: str, session_id: str, url: str, draft_id: str,
) -> dict[str, Any]:
    """안전 입력 요청을 발급한다. 돌려주는 값에는 비밀값이 없다.

    SecureInputError 는 그대로 올린다(호출부가 HTTP 상태로 옮긴다).
    """
    if not vault_secure_input.is_enabled():
        raise vault_secure_input.SecureInputError(404, "secure_input_disabled")
    parsed = urlparse(url or "")
    login_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme and parsed.netloc else url
    result = await vault_secure_input.request_credential_input(
        tenant_id=tenant_id,
        session_id=session_id,
        url=login_url,
        browser_work_key=f"automation-draft:{draft_id}",
        reason="업무 자동화 로그인 연결",
    )
    return {
        "request_id": result["request_id"],
        "requestId": result["request_id"],
        "host": result.get("host", ""),
        "reused": bool(result.get("reused")),
        "expires_in_minutes": REQUEST_TTL_MINUTES,
    }


async def request_progress(*, tenant_id: str, request_id: str) -> dict[str, Any]:
    """보안 입력 요청의 현재 상태 → {'status', 'credential_id'}. 알 수 없는 요청은 status='unknown'."""
    try:
        view = await vault_secure_input.get_credential_request(tenant_id=tenant_id, request_id=request_id)
    except vault_secure_input.SecureInputError:
        return {"status": "unknown", "credential_id": None}
    return {"status": str(view.get("status") or "unknown"), "credential_id": view.get("credential_id")}
