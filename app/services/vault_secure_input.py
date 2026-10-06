"""Vault 미등록 사이트 → 보안 입력 요청 카드 → CEO 직접 입력 → Vault 저장.

비밀번호는 채팅·도구 결과·로그·요청 테이블 어디에도 남지 않는다. 요청 테이블
(agent_vault_credential_requests)에는 비밀값 컬럼이 없고, 비밀번호는 submit 호출
안에서만 살아 agent_vault_service 의 암호화 저장 함수로 곧바로 넘어간다.
요청 상태는 DB 에 있으므로 blue/green 배포로 사라지지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import asyncpg

logger = logging.getLogger(__name__)

ACTION_TYPE = "vault_credential_input"
GATE_SOURCE = "vault_credential_input"
VAULT_WORK_KEY = "aads-ceo-browser"
SOURCE = "chat_secure_input"
REQUEST_TTL_MINUTES = 30
RATE_ACTION = "credential_input_submit_attempt"
DEFAULT_RATE_PER_MINUTE = 10
MAX_USERNAME_LEN = 500
MAX_PASSWORD_LEN = 2000
MAX_LABEL_LEN = 120
MAX_REASON_LEN = 200
VERIFY_TTL_SECONDS = 600
VERIFY_MAX_CHECKS = 3

_FALSE_VALUES = {"0", "false", "off", "no"}
_SESSION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_clock = time.monotonic


def is_enabled() -> bool:
    return os.getenv("VAULT_SECURE_INPUT_ENABLED", "1").strip().lower() not in _FALSE_VALUES


def _rate_limit_per_minute() -> int:
    try:
        return max(1, int(os.getenv("VAULT_SECURE_INPUT_RATE_PER_MIN", str(DEFAULT_RATE_PER_MINUTE))))
    except ValueError:
        return DEFAULT_RATE_PER_MINUTE


class SecureInputError(Exception):
    """HTTP 상태로 옮길 수 있는 오류. 메시지에는 비밀값을 절대 담지 않는다."""

    def __init__(self, status_code: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.extra = extra


def safe_login_url(url: str) -> str:
    """쿼리·프래그먼트·사용자 정보를 뗀 URL. 토큰이 붙은 URL 이 카드에 남지 않게 한다."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.netloc.rsplit("@", 1)[-1].lower()
    return f"{parsed.scheme.lower()}://{host}{parsed.path or ''}"[:500]


def _clean_reason(reason: str) -> str:
    return " ".join(str(reason or "").split())[:MAX_REASON_LEN]


def _tenant_uuid(tenant_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(tenant_id or "").strip())
    except ValueError as exc:
        raise SecureInputError(400, "tenant_required") from exc


def _request_uuid(request_id: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(request_id or "").strip())
    except ValueError:
        return None


# ── 요청 생성 ────────────────────────────────────────────────────────

_EXPIRE_SQL = """
UPDATE agent_vault_credential_requests
   SET status = 'expired', updated_at = now()
 WHERE tenant_id = $1 AND session_id = $2 AND origin = $3
   AND status = 'pending' AND expires_at <= now()
"""

_FIND_PENDING_SQL = """
SELECT id::text AS id, permission_request_id::text AS permission_request_id
  FROM agent_vault_credential_requests
 WHERE tenant_id = $1 AND session_id = $2 AND origin = $3
   AND status = 'pending' AND expires_at > now()
 ORDER BY created_at DESC LIMIT 1
"""

_INSERT_CARD_SQL = """
INSERT INTO agent_permission_requests
    (tenant_id, work_key, origin, action_type, action_summary, risk_level, decision,
     reason, requested_by, approval_scope, max_executions, expires_at, created_at,
     gate_source, tier)
VALUES ($1, $2, $3, $4, $5, 'medium', 'pending', $6, $7, $8::jsonb, 1,
        now() + make_interval(mins => $9), now(), $10, 'approve')
RETURNING id::text
"""

_INSERT_REQUEST_SQL = """
INSERT INTO agent_vault_credential_requests
    (id, tenant_id, session_id, origin, login_url, browser_work_key, status,
     permission_request_id, reason, expires_at, created_at, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7::uuid, $8,
        now() + make_interval(mins => $9), now(), now())
"""


def _summary(host: str, login_url: str) -> str:
    return f"Vault 에 없는 사이트 계정 입력 요청 · {host} · {login_url or host}"


async def request_credential_input(
    *,
    tenant_id: str,
    session_id: str,
    url: str,
    browser_work_key: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """입력 요청과 채팅 카드를 만든다. 같은 tenant·session·origin 의 pending 요청이 있으면 재사용."""
    from app.core.db_pool import get_pool
    from app.services.browser_login_autosave import host_of, origin_of

    origin = origin_of(url)
    if not origin:
        raise SecureInputError(400, "invalid_url")
    tenant = _tenant_uuid(tenant_id)
    session = str(session_id or "").strip()
    host = host_of(origin)
    login_url = safe_login_url(url) or origin
    note = _clean_reason(reason)
    work_key = str(browser_work_key or "").strip()[:200]

    async def _reuse(conn: Any) -> Optional[dict[str, Any]]:
        row = await conn.fetchrow(_FIND_PENDING_SQL, tenant, session, origin)
        if not row:
            return None
        return {
            "request_id": row["id"], "permission_request_id": row["permission_request_id"],
            "origin": origin, "host": host, "login_url": login_url, "reused": True,
        }

    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute(_EXPIRE_SQL, tenant, session, origin)
                reused = await _reuse(conn)
                if reused:
                    return reused
                request_id = uuid.uuid4()
                scope = {
                    "scope": "single_call", "credential_request_id": str(request_id),
                    "origin": origin, "host": host, "login_url": login_url, "reason": note,
                }
                card_id = await conn.fetchval(
                    _INSERT_CARD_SQL,
                    tenant, f"{GATE_SOURCE}:{request_id}", origin, ACTION_TYPE,
                    _summary(host, login_url), f"vault_credential_request_id={request_id}",
                    session, json.dumps(scope, ensure_ascii=False), REQUEST_TTL_MINUTES, GATE_SOURCE,
                )
                await conn.execute(
                    _INSERT_REQUEST_SQL,
                    request_id, tenant, session, origin, login_url, work_key,
                    str(card_id), note, REQUEST_TTL_MINUTES,
                )
        except asyncpg.UniqueViolationError:
            reused = await _reuse(conn)
            if reused:
                return reused
            raise
    logger.info("vault_credential_input_requested request=%s host=%s", str(request_id)[:8], host)
    return {
        "request_id": str(request_id), "permission_request_id": str(card_id),
        "origin": origin, "host": host, "login_url": login_url, "reused": False,
    }


def tool_result_text(result: dict[str, Any]) -> str:
    """에이전트에게 돌려줄 도구 결과. 비밀값은 없다."""
    return (
        f"credential_input_requested request_id={result['request_id']} — 대표님 입력 대기\n"
        f"{result.get('host', '')} 에 Vault 계정이 없어 채팅에 보안 입력 카드를 올렸습니다"
        f"{' (이미 올라간 카드를 재사용)' if result.get('reused') else ''}. "
        "대표님이 카드에 아이디·비밀번호를 입력하면 시스템 알림이 옵니다. "
        "입력 완료 후 같은 selector 에 {{vault:username}}/{{vault:password}} 로 다시 fill 하십시오. "
        "비밀번호를 채팅에 묻거나 value 에 직접 쓰지 마십시오."
    )


# ── 조회 ─────────────────────────────────────────────────────────────

_SELECT_REQUEST_SQL = """
SELECT id::text AS id, tenant_id::text AS tenant_id, session_id, origin, login_url,
       browser_work_key, status, credential_id::text AS credential_id,
       permission_request_id::text AS permission_request_id, reason,
       expires_at, created_at, updated_at, (expires_at <= now()) AS is_expired
  FROM agent_vault_credential_requests
 WHERE id = $1
"""


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else None


async def _load_request(conn: Any, tenant_id: str, request_id: str) -> dict[str, Any]:
    rid = _request_uuid(request_id)
    if rid is None:
        raise SecureInputError(404, "request_not_found")
    row = await conn.fetchrow(_SELECT_REQUEST_SQL, rid)
    if not row:
        raise SecureInputError(404, "request_not_found")
    if str(row["tenant_id"]) != str(_tenant_uuid(tenant_id)):
        raise SecureInputError(403, "tenant_mismatch")
    return dict(row)


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    from app.services.browser_login_autosave import host_of

    status = str(row["status"])
    if status == "pending" and row.get("is_expired"):
        status = "expired"
    return {
        "id": row["id"], "status": status, "origin": row["origin"],
        "host": host_of(row["origin"]), "login_url": row["login_url"],
        "session_id": row["session_id"], "credential_id": row.get("credential_id"),
        "permission_request_id": row.get("permission_request_id"),
        "reason": row.get("reason") or "",
        "expires_at": _iso(row.get("expires_at")), "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


async def get_credential_request(*, tenant_id: str, request_id: str) -> dict[str, Any]:
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        return _public_view(await _load_request(conn, tenant_id, request_id))


# ── 제출 ─────────────────────────────────────────────────────────────

_RATE_COUNT_SQL = """
SELECT count(*) FROM agent_vault_access_logs
 WHERE tenant_id = $1 AND action = $2 AND created_at > now() - interval '60 seconds'
"""

_CLAIM_SQL = """
UPDATE agent_vault_credential_requests
   SET status = 'submitted', updated_at = now()
 WHERE id = $1 AND tenant_id = $2 AND status = 'pending' AND expires_at > now()
RETURNING id::text AS id
"""

_REVERT_SQL = """
UPDATE agent_vault_credential_requests
   SET status = 'pending', updated_at = now()
 WHERE id = $1 AND status = 'submitted' AND credential_id IS NULL
"""

_RECORD_CREDENTIAL_SQL = """
UPDATE agent_vault_credential_requests
   SET credential_id = $2::uuid, updated_at = now()
 WHERE id = $1
"""

_DECIDE_CARD_SQL = """
UPDATE agent_permission_requests
   SET decision = $3, decided_by = $4, decided_at = now(),
       approval_scope = COALESCE(approval_scope, '{}'::jsonb) || '{"used": 1}'::jsonb,
       expires_at = now(), updated_at = now()
 WHERE id = $1::uuid AND tenant_id = $2 AND decision = 'pending'
"""


async def _enforce_rate_limit(tenant_id: str, user_id: str) -> None:
    from app.core.db_pool import get_pool
    from app.services import agent_vault_service as vault

    tenant = _tenant_uuid(tenant_id)
    async with get_pool().acquire() as conn:
        count = await conn.fetchval(_RATE_COUNT_SQL, tenant, RATE_ACTION)
        if int(count or 0) >= _rate_limit_per_minute():
            raise SecureInputError(429, "rate_limited", retry_after=60)
        await vault.write_access_log(
            conn=conn, tenant_id=str(tenant), credential_id=None, work_key="", origin="",
            action=RATE_ACTION, status="attempt", user_id=user_id, details={},
        )


async def _free_label(vault: Any, *, tenant_id: str, origin: str, label: str, username: str) -> str:
    base = (label or "").strip()[:MAX_LABEL_LEN] or f"{origin.split('://', 1)[-1]} - {username}"[:MAX_LABEL_LEN]
    taken = {
        str(c.get("label") or "")
        for c in await vault.list_agent_credentials(
            tenant_id=tenant_id, work_key=VAULT_WORK_KEY, origin=origin,
        )
    }
    if base not in taken:
        return base
    return f"{base[:MAX_LABEL_LEN - 8]} #{secrets.token_hex(3)}"


async def _save_credential(
    *, tenant_id: str, user_id: str, origin: str, username: str, password: str, label: str,
) -> tuple[str, str]:
    """암호화 저장. (credential_id, 'saved'|'updated')."""
    from app.services import agent_vault_service as vault

    existing = await vault.find_agent_credential_by_username(
        tenant_id=tenant_id, origin=origin, username=username,
    )
    if existing:
        metadata = {**(existing.get("metadata") or {}), "source": SOURCE, "verification_status": "unverified"}
        metadata.setdefault("policy", "ask")
        saved = await vault.update_agent_credential(
            tenant_id=tenant_id, user_id=user_id, credential_id=existing["id"],
            work_key=existing["work_key"], origin=origin, label=existing["label"],
            username=username, password=password, metadata=metadata,
        )
        status = "updated"
    else:
        saved = await vault.upsert_agent_credential(
            tenant_id=tenant_id, user_id=user_id, work_key=VAULT_WORK_KEY, origin=origin,
            label=await _free_label(vault, tenant_id=tenant_id, origin=origin, label=label, username=username),
            username=username, password=password,
            metadata={"source": SOURCE, "policy": "ask", "verification_status": "unverified"},
        )
        status = "saved"
    if not saved or not saved.get("id"):
        raise RuntimeError("credential_not_saved")
    return str(saved["id"]), status


async def _notify_session(session_id: str, text: str) -> str:
    sid = (session_id or "").strip()
    if not _SESSION_UUID_RE.match(sid):
        return "skipped"
    try:
        from app.services.chat_service import trigger_ai_reaction

        await trigger_ai_reaction(sid, text)
        return "notified"
    except Exception as exc:  # noqa: BLE001
        logger.warning("vault_credential_input_notify_failed: %s", type(exc).__name__)
        return "failed"


async def submit_credential(
    *,
    tenant_id: str,
    user_id: str,
    request_id: str,
    username: str,
    password: str,
    label: str = "",
) -> dict[str, Any]:
    """CEO 가 카드에 입력한 계정을 1회만 Vault 에 암호화 저장한다. 응답에 비밀값은 없다."""
    from app.core.db_pool import get_pool
    from app.services.browser_login_autosave import host_of

    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password or len(username) > MAX_USERNAME_LEN or len(password) > MAX_PASSWORD_LEN:
        raise SecureInputError(422, "invalid_input")
    tenant = _tenant_uuid(tenant_id)
    await _enforce_rate_limit(tenant_id, user_id)

    async with get_pool().acquire() as conn:
        row = await _load_request(conn, tenant_id, request_id)
        if row["status"] == "expired" or (row["status"] == "pending" and row.get("is_expired")):
            raise SecureInputError(410, "request_expired")
        if row["status"] != "pending":
            raise SecureInputError(409, "request_not_pending", status=row["status"])
        claimed = await conn.fetchrow(_CLAIM_SQL, _request_uuid(request_id), tenant)
        if not claimed:
            latest = await _load_request(conn, tenant_id, request_id)
            if latest["status"] == "pending" and latest.get("is_expired"):
                raise SecureInputError(410, "request_expired")
            raise SecureInputError(409, "request_not_pending", status=latest["status"])

    origin = str(row["origin"])
    host = host_of(origin)
    try:
        credential_id, mode = await _save_credential(
            tenant_id=tenant_id, user_id=user_id, origin=origin,
            username=username, password=password, label=str(label or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("vault_credential_input_save_failed: %s", type(exc).__name__)
        try:
            async with get_pool().acquire() as conn:
                await conn.execute(_REVERT_SQL, _request_uuid(request_id))
        except Exception as revert_exc:  # noqa: BLE001
            logger.warning("vault_credential_input_revert_failed: %s", type(revert_exc).__name__)
        raise SecureInputError(503, "credential_save_failed") from None

    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(_RECORD_CREDENTIAL_SQL, _request_uuid(request_id), credential_id)
                if row.get("permission_request_id"):
                    await conn.execute(
                        _DECIDE_CARD_SQL, row["permission_request_id"], tenant, "approved", str(user_id),
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("vault_credential_input_finalize_failed: %s", type(exc).__name__)

    chat = await _notify_session(
        str(row["session_id"]),
        f"[시스템] {host} 계정 입력 완료 — 자동 로그인을 이어서 진행합니다.\n"
        "같은 selector 에 {{vault:username}}/{{vault:password}} 로 fill 하십시오. "
        "비밀값은 채팅에 없습니다.",
    )
    logger.info("vault_credential_input_submitted request=%s host=%s mode=%s", request_id[:8], host, mode)
    return {
        "status": "submitted", "request_id": str(request_id), "credential_id": credential_id,
        "mode": mode, "origin": origin, "host": host, "verification_status": "unverified",
        "chat_notified": chat,
    }


# ── 취소 ─────────────────────────────────────────────────────────────

_CANCEL_SQL = """
UPDATE agent_vault_credential_requests
   SET status = 'cancelled', updated_at = now()
 WHERE id = $1 AND tenant_id = $2 AND status = 'pending'
RETURNING permission_request_id::text AS permission_request_id
"""

_CANCEL_BY_CARD_SQL = """
UPDATE agent_vault_credential_requests
   SET status = 'cancelled', updated_at = now()
 WHERE permission_request_id = $1::uuid AND tenant_id = $2 AND status = 'pending'
"""


async def cancel_credential_request(
    *, tenant_id: str, user_id: str, request_id: str, notify: bool = True,
) -> dict[str, Any]:
    from app.core.db_pool import get_pool
    from app.services.browser_login_autosave import host_of

    tenant = _tenant_uuid(tenant_id)
    async with get_pool().acquire() as conn:
        row = await _load_request(conn, tenant_id, request_id)
        if row["status"] == "expired" or (row["status"] == "pending" and row.get("is_expired")):
            raise SecureInputError(410, "request_expired")
        if row["status"] != "pending":
            raise SecureInputError(409, "request_not_pending", status=row["status"])
        done = await conn.fetchrow(_CANCEL_SQL, _request_uuid(request_id), tenant)
        if not done:
            raise SecureInputError(409, "request_not_pending")
        if row.get("permission_request_id"):
            await conn.execute(
                _DECIDE_CARD_SQL, row["permission_request_id"], tenant, "rejected", str(user_id),
            )
    host = host_of(row["origin"])
    if notify:
        await _notify_session(
            str(row["session_id"]),
            f"[시스템] 대표님이 {host} 계정 입력을 취소했습니다 — 이 사이트 로그인은 진행하지 마세요.",
        )
    return {"status": "cancelled", "request_id": str(request_id), "origin": row["origin"], "host": host}


async def cancel_by_card(*, tenant_id: str, permission_request_id: str) -> None:
    """승인 화면의 '거절' 로 카드가 닫힐 때 짝이 되는 입력 요청도 닫는다."""
    from app.core.db_pool import get_pool

    await get_pool().execute(_CANCEL_BY_CARD_SQL, permission_request_id, _tenant_uuid(tenant_id))


# ── 로그인 검증 ──────────────────────────────────────────────────────

@dataclass
class _Awaiting:
    credential_id: str
    login_url: str
    expires_at: float
    checks_left: int = VERIFY_MAX_CHECKS


_awaiting: dict[tuple[str, str, str], _Awaiting] = {}

_REQUEST_VERIFIED_SQL = """
UPDATE agent_vault_credential_requests
   SET status = 'verified', updated_at = now()
 WHERE tenant_id = $1 AND credential_id = $2::uuid AND status IN ('submitted', 'failed')
"""

_REQUEST_FAILED_SQL = """
UPDATE agent_vault_credential_requests
   SET status = 'failed', reason = $3, updated_at = now()
 WHERE tenant_id = $1 AND credential_id = $2::uuid AND status = 'submitted'
"""


def clear_awaiting() -> None:
    _awaiting.clear()


def _purge(now: float) -> None:
    for key in [k for k, v in _awaiting.items() if v.expires_at <= now]:
        _awaiting.pop(key, None)


def note_vault_fill(
    *, tenant_id: str, session_id: str, origin: str, credential_id: str, field: str, login_url: str,
) -> None:
    """Vault 참조로 비밀번호 칸을 채웠다 — 다음 제출 뒤 로그인 성공 여부를 판정할 표식."""
    if field != "password" or not (tenant_id and origin and credential_id):
        return
    now = _clock()
    _purge(now)
    _awaiting[(tenant_id, session_id, origin)] = _Awaiting(
        credential_id=credential_id, login_url=login_url, expires_at=now + VERIFY_TTL_SECONDS,
    )


def has_awaiting(tenant_id: str, session_id: str, origin: str) -> bool:
    _purge(_clock())
    return (tenant_id, session_id, origin) in _awaiting


async def _mark(tenant_id: str, credential_id: str, *, ok: bool, reason: str = "") -> bool:
    from app.core.db_pool import get_pool
    from app.services import agent_vault_service as vault

    changed = await vault.set_credential_verification(
        tenant_id=tenant_id, credential_id=credential_id,
        status="verified" if ok else "failed",
        from_statuses=("unverified", "failed") if ok else ("unverified",),
    )
    tenant = _tenant_uuid(tenant_id)
    if ok:
        await get_pool().execute(_REQUEST_VERIFIED_SQL, tenant, credential_id)
    else:
        await get_pool().execute(_REQUEST_FAILED_SQL, tenant, credential_id, _clean_reason(reason))
    return changed


async def on_submit_verify(
    *,
    page: Any,
    origin: str,
    tenant_id: str,
    session_id: str,
    wait_login_completed: Callable[[Any, str], Awaitable[bool]],
) -> str:
    """Vault 참조로 채운 로그인의 성공·실패를 판정해 credential·요청 상태에 반영한다."""
    key = (tenant_id, session_id, origin)
    if not has_awaiting(*key):
        return ""
    entry = _awaiting[key]
    entry.checks_left -= 1
    if entry.checks_left <= 0:
        _awaiting.pop(key, None)
    if await wait_login_completed(page, entry.login_url):
        _awaiting.pop(key, None)
        changed = await _mark(tenant_id, entry.credential_id, ok=True)
        return " (Vault 계정 로그인 확인 — 검증됨으로 표시했습니다)" if changed else ""
    changed = await _mark(tenant_id, entry.credential_id, ok=False, reason="login_not_completed")
    return " (Vault 계정으로 로그인이 확인되지 않아 검증 실패로 표시했습니다)" if changed else ""
