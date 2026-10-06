"""스마트브라우저 로그인 입력 → Agent Vault 저장 제안(승인 카드).

browser_fill 로 넣은 아이디·비밀번호를 서버 메모리 슬롯에만 잠시 들고 있다가,
제출 뒤 로그인 성공이 판정되면 "저장할까요?" 승인 카드를 올린다. CEO 가 승인하면
그때 슬롯의 비밀번호로 Vault 에 저장한다.

비밀번호 원문은 이 모듈의 슬롯(프로세스 메모리) 밖으로 나가지 않는다 — DB·로그·
카드 payload·tool 결과 어디에도 없다. 카드에는 슬롯 참조 id 만 들어간다.
uvicorn 은 --workers 1 이라 슬롯과 승인 처리기가 같은 프로세스에 있다.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SLOT_TTL_SECONDS = 600
MAX_SLOTS = 200
MAX_VALUE_LEN = 2000
ACTION_TYPE = "save_browser_credential"
GATE_SOURCE = "browser_login_save"
VAULT_WORK_KEY = "aads-ceo-browser"

_SETTLE_ATTEMPTS = 3
_SETTLE_DELAY_SECONDS = 1.0
_PROBE_TIMEOUT_SECONDS = 2.0
_SUBMIT_KEYS = {"enter", "return"}

_PW_HINT = re.compile(
    r"passw|pwd|current-password|new-password|비밀번호|(?<![a-z])pw(?![a-z])", re.I,
)
_PW_CAMEL_HINT = re.compile(r"[a-z]Pw(?![a-z])")
_PW_SELECTOR_TYPE = re.compile(r"type\s*=\s*['\"]?password", re.I)
VAULT_REF_RE = re.compile(r"^\s*\{\{\s*vault\s*:\s*(password|username)\s*\}\}\s*$", re.I)
MASKED = "***MASKED***"
_SECRET_KEY_RE = re.compile(r"password|passwd|secret|(?<![a-z])token(?![a-z])", re.I)
_USER_HINT = re.compile(r"user|e-?mail|login|account|identifier|signin", re.I)

_clock = time.monotonic


@dataclass
class _Slot:
    id: str
    tenant_id: str
    session_id: str
    origin: str
    login_url: str
    expires_at: float
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    request_id: str = ""


_slots: dict[tuple[str, str, str], _Slot] = {}


def _purge(now: Optional[float] = None) -> None:
    now = _clock() if now is None else now
    for key in [k for k, s in _slots.items() if s.expires_at <= now]:
        _slots.pop(key, None)


def _find_by_id(slot_id: str) -> Optional[_Slot]:
    _purge()
    for slot in _slots.values():
        if slot.id == slot_id:
            return slot
    return None


def origin_of(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def host_of(origin: str) -> str:
    return urlparse(origin).netloc


def mask_username(username: str) -> str:
    text = (username or "").strip()
    if not text:
        return "***"
    local, sep, domain = text.partition("@")
    keep = 2 if len(local) > 3 else 1
    return f"{local[:keep]}***{sep}{domain}"


def parse_vault_ref(value: Any) -> str:
    """'{{vault:password}}' / '{{vault:username}}' 이면 'password' / 'username', 아니면 ''."""
    match = VAULT_REF_RE.match(value) if isinstance(value, str) else None
    return match.group(1).lower() if match else ""


def is_password_field(selector: str, attrs: Optional[dict[str, Any]] = None) -> bool:
    attrs = attrs or {}
    hay = " ".join(
        str(attrs.get(k) or "") for k in ("name", "id", "autocomplete", "placeholder")
    ) + " " + (selector or "")
    return bool(
        str(attrs.get("type") or "").lower() == "password"
        or _PW_HINT.search(hay)
        or _PW_CAMEL_HINT.search(hay)
        or _PW_SELECTOR_TYPE.search(selector or "")
    )


def classify_field(selector: str, attrs: Optional[dict[str, Any]] = None) -> str:
    """'password' | 'username' | '' — 입력한 필드가 무엇인지."""
    attrs = attrs or {}
    ftype = str(attrs.get("type") or "").lower()
    hay = " ".join(
        str(attrs.get(k) or "") for k in ("name", "id", "autocomplete", "placeholder")
    ) + " " + (selector or "")
    if is_password_field(selector, attrs):
        return "password"
    if ftype in ("hidden", "checkbox", "radio", "file", "number", "search"):
        return ""
    if ftype == "email" or _USER_HINT.search(hay):
        return "username"
    return ""


def _mask_step_values(steps: Any) -> Any:
    """login_steps 처럼 {selector, value} 를 담은 목록에서 비밀번호 칸 value 를 가린다."""
    if not isinstance(steps, list):
        return steps
    out = []
    for step in steps:
        if isinstance(step, dict) and "value" in step and not parse_vault_ref(step.get("value")):
            if is_password_field(str(step.get("selector") or "")):
                step = {**step, "value": MASKED}
        out.append(mask_secret_values(step))
    return out


def mask_secret_values(value: Any) -> Any:
    """password/passwd/secret/token 키의 값을 재귀적으로 가린다. Vault 참조 문자열은 유지."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)) and item not in (None, "") and not parse_vault_ref(item):
                out[key] = MASKED
            else:
                out[key] = mask_secret_values(item)
        return out
    if isinstance(value, list):
        return [mask_secret_values(item) for item in value]
    return value


def mask_tool_input(tool_name: str, tool_input: Any) -> Any:
    """chat_messages.tools_called 에 저장하기 직전 tool_input 에서 비밀값을 가린다."""
    if not isinstance(tool_input, dict) or not tool_input:
        return tool_input
    name = str(tool_name or "")
    masked = mask_secret_values(tool_input)
    if name == "browser_fill":
        value = tool_input.get("value")
        if (
            isinstance(value, str) and value and not parse_vault_ref(value)
            and is_password_field(str(tool_input.get("selector") or ""))
        ):
            masked["value"] = MASKED
    if name.startswith("credential") or name.startswith("agent_vault"):
        extra = tool_input.get("extra_fields")
        if isinstance(extra, dict):
            masked["extra_fields"] = {k: (MASKED if v not in (None, "") else v) for k, v in extra.items()}
        elif extra:
            masked["extra_fields"] = MASKED
        if "login_steps" in tool_input:
            masked["login_steps"] = _mask_step_values(masked.get("login_steps"))
    return masked


def _slot_key(tenant_id: str, session_id: str, origin: str) -> tuple[str, str, str]:
    return (tenant_id, session_id, origin)


def record_fill(
    *,
    tenant_id: str,
    session_id: str,
    page_url: str,
    selector: str,
    value: str,
    attrs: Optional[dict[str, Any]] = None,
) -> str:
    """채운 값을 슬롯에 담는다. 담은 필드 종류('password'/'username')를 돌려준다."""
    origin = origin_of(page_url)
    if not tenant_id or not origin or not value or len(value) > MAX_VALUE_LEN:
        return ""
    kind = classify_field(selector, attrs)
    if not kind:
        return ""
    now = _clock()
    _purge(now)
    key = _slot_key(tenant_id, session_id, origin)
    slot = _slots.get(key)
    if kind == "username":
        if slot is None:
            if len(_slots) >= MAX_SLOTS:
                return ""
            slot = _Slot(
                id=secrets.token_urlsafe(12), tenant_id=tenant_id, session_id=session_id,
                origin=origin, login_url=page_url, expires_at=now + SLOT_TTL_SECONDS,
            )
            _slots[key] = slot
        slot.username = value
        return "username"
    if slot is None and len(_slots) >= MAX_SLOTS:
        return ""
    # 비밀번호가 바뀌면 새 슬롯 id 를 쓴다 — 이전 비밀번호로 올라간 카드가
    # 새 비밀번호를 저장하는 일이 없게 한다.
    _slots[key] = _Slot(
        id=secrets.token_urlsafe(12), tenant_id=tenant_id, session_id=session_id,
        origin=origin, login_url=page_url, expires_at=now + SLOT_TTL_SECONDS,
        username=slot.username if slot else "", password=value,
    )
    return "password"


def has_pending_slot(tenant_id: str, session_id: str, origin: str) -> bool:
    _purge()
    slot = _slots.get(_slot_key(tenant_id, session_id, origin))
    return bool(slot and slot.password and slot.username and not slot.request_id)


def discard(slot_id: str) -> bool:
    for key, slot in list(_slots.items()):
        if slot.id == slot_id:
            _slots.pop(key, None)
            return True
    return False


def discard_origin(tenant_id: str, session_id: str, origin: str) -> None:
    _slots.pop(_slot_key(tenant_id, session_id, origin), None)


def clear_all() -> None:
    _slots.clear()


# ── 브라우저 도구 연동 ───────────────────────────────────────────────

def _bound_context(tenant_id: str, fallback_session: str) -> tuple[str, str]:
    session_id = ""
    try:
        from app.services.tool_executor import (
            _resolve_bound_chat_session_id,
            current_tenant_id,
        )

        session_id = _resolve_bound_chat_session_id("")
        if not tenant_id:
            tenant_id = str(current_tenant_id.get("") or "")
    except Exception:  # noqa: BLE001
        pass
    return str(tenant_id or "").strip(), str(session_id or fallback_session or "").strip()


def page_origin(page: Any) -> str:
    return origin_of(str(getattr(page, "url", "") or ""))


async def _probe_attrs(page: Any, selector: str) -> dict[str, Any]:
    probe = getattr(page, "eval_on_selector", None)
    if not callable(probe) or callable(getattr(page, "_run_browser_command", None)):
        return {}
    try:
        result = await asyncio.wait_for(
            probe(
                selector,
                "el => ({type: el.type || '', name: el.name || '', id: el.id || '',"
                " autocomplete: el.autocomplete || '', placeholder: el.placeholder || ''})",
            ),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001
        return {}
    return result if isinstance(result, dict) else {}


async def on_fill(
    page: Any, selector: str, value: str, *, tenant_id: str = "", fallback_session: str = "",
    from_vault: bool = False, vault_credential_id: str = "", vault_field: str = "",
) -> None:
    """browser_fill 성공 직후 호출. 어떤 실패도 도구 결과에 영향을 주지 않는다.

    from_vault=True 는 "vault 자동입력" — 이미 Vault 에 있는 계정이므로 슬롯을 만들지
    않고, 이 origin 에 남은 슬롯이 있으면 버려 저장 제안 카드가 생기지 않게 한다.
    """
    try:
        tenant_id, session_id = _bound_context(tenant_id, fallback_session)
        if not tenant_id or not value:
            return
        if from_vault:
            discard_origin(tenant_id, session_id, page_origin(page))
            if vault_credential_id:
                from app.services import vault_secure_input

                vault_secure_input.note_vault_fill(
                    tenant_id=tenant_id, session_id=session_id, origin=page_origin(page),
                    credential_id=vault_credential_id, field=vault_field,
                    login_url=str(getattr(page, "url", "") or ""),
                )
            return
        attrs = await _probe_attrs(page, selector)
        record_fill(
            tenant_id=tenant_id, session_id=session_id,
            page_url=str(getattr(page, "url", "") or ""),
            selector=selector, value=value, attrs=attrs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_autosave_fill_failed: %s", type(exc).__name__)


def is_submit_key(key: str) -> bool:
    return (key or "").strip().lower() in _SUBMIT_KEYS


async def _wait_login_completed(page: Any, login_url: str) -> bool:
    from app.core.credential_vault import login_session_completed

    is_pc_page = callable(getattr(page, "_run_browser_command", None))
    for attempt in range(_SETTLE_ATTEMPTS):
        if await login_session_completed(page, login_url):
            return True
        if is_pc_page or attempt == _SETTLE_ATTEMPTS - 1:
            break
        await asyncio.sleep(_SETTLE_DELAY_SECONDS)
    return False


async def on_submit(
    page: Any, origin_before: str, *, tenant_id: str = "", fallback_session: str = "",
) -> str:
    """클릭/Enter 직후 호출. 제안 카드를 올렸으면 도구 결과에 붙일 안내 문구를 돌려준다."""
    try:
        tenant_id, session_id = _bound_context(tenant_id, fallback_session)
        if not tenant_id or not origin_before:
            return ""
        verify_note = await _verify_vault_login(page, origin_before, tenant_id, session_id)
        if not has_pending_slot(tenant_id, session_id, origin_before):
            return verify_note
        slot = _slots[_slot_key(tenant_id, session_id, origin_before)]
        if not await _wait_login_completed(page, slot.login_url):
            return verify_note
        result = await propose_save(tenant_id, session_id, origin_before)
        if result.get("status") == "proposed":
            return verify_note + " (로그인 계정을 Agent Vault 에 저장할지 승인 카드를 올렸습니다)"
        return verify_note
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_autosave_submit_failed: %s", type(exc).__name__)
    return ""


async def _verify_vault_login(page: Any, origin: str, tenant_id: str, session_id: str) -> str:
    """보안 입력으로 저장된 계정으로 로그인했다면 성공·실패를 credential 상태에 반영한다."""
    try:
        from app.services import vault_secure_input

        if not vault_secure_input.has_awaiting(tenant_id, session_id, origin):
            return ""
        return await vault_secure_input.on_submit_verify(
            page=page, origin=origin, tenant_id=tenant_id, session_id=session_id,
            wait_login_completed=_wait_login_completed,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_vault_verify_failed: %s", type(exc).__name__)
        return ""


# ── 제안 카드 ────────────────────────────────────────────────────────

def _scope_key(origin: str, username: str) -> str:
    digest = hashlib.sha256(f"{origin}\0{username}".encode("utf-8")).hexdigest()[:16]
    return f"{GATE_SOURCE}:{digest}"


def _summary(mode: str, host: str, masked: str) -> str:
    verb = "업데이트" if mode == "update" else "저장"
    return f"브라우저 로그인 계정을 Agent Vault 에 {verb}할까요? · {host} · {masked}"


async def propose_save(tenant_id: str, session_id: str, origin: str) -> dict[str, Any]:
    """로그인 성공이 판정된 슬롯으로 승인 카드를 올린다. 비밀번호는 카드에 넣지 않는다."""
    _purge()
    key = _slot_key(tenant_id, session_id, origin)
    slot = _slots.get(key)
    if not slot or not slot.password or not slot.username or slot.request_id:
        return {"status": "no_slot"}

    from app.core.db_pool import get_pool
    from app.services.agent_vault_service import find_agent_credential_by_username

    existing = await find_agent_credential_by_username(
        tenant_id=tenant_id, origin=origin, username=slot.username,
    )
    mode = "new"
    if existing:
        if hmac.compare_digest(
            str(existing.get("password") or "").encode("utf-8"),
            slot.password.encode("utf-8"),
        ):
            discard(slot.id)
            return {"status": "already_saved"}
        mode = "update"

    host = host_of(origin)
    masked = mask_username(slot.username)
    summary = _summary(mode, host, masked)
    scope = {
        "scope": "single_call", "slot_id": slot.id, "origin": origin, "host": host,
        "username_masked": masked, "mode": mode,
    }
    work_key = _scope_key(origin, slot.username)
    remaining = max(1, int(slot.expires_at - _clock()))
    pool = get_pool()
    async with pool.acquire() as conn:
        request_id = await conn.fetchval(
            "SELECT id::text FROM agent_permission_requests "
            " WHERE tenant_id = $1::uuid AND work_key = $2 AND decision = 'pending' "
            "   AND expires_at > now() ORDER BY created_at DESC LIMIT 1",
            tenant_id, work_key,
        )
        if request_id:
            await conn.execute(
                "UPDATE agent_permission_requests "
                "   SET approval_scope = $2::jsonb, action_summary = $3, "
                "       expires_at = now() + make_interval(secs => $4), updated_at = now() "
                " WHERE id = $1::uuid",
                request_id, json.dumps(scope, ensure_ascii=False), summary, float(remaining),
            )
        else:
            request_id = await conn.fetchval(
                """
                INSERT INTO agent_permission_requests
                    (tenant_id, work_key, origin, action_type, action_summary,
                     risk_level, decision, requested_by, approval_scope,
                     max_executions, expires_at, created_at, gate_source, tier)
                VALUES ($1::uuid, $2, $3, $4, $5, 'medium', 'pending', $6, $7::jsonb,
                        1, now() + make_interval(secs => $8), now(), $9, 'approve')
                RETURNING id::text
                """,
                tenant_id, work_key, origin, ACTION_TYPE, summary, session_id,
                json.dumps(scope, ensure_ascii=False), float(remaining), GATE_SOURCE,
            )
    slot.request_id = str(request_id)
    logger.info(
        "browser_autosave_proposed request=%s host=%s mode=%s", str(request_id)[:8], host, mode,
    )
    return {"status": "proposed", "request_id": str(request_id), "mode": mode}


async def _consume_card(request_id: str) -> None:
    from app.core.db_pool import get_pool

    await get_pool().execute(
        "UPDATE agent_permission_requests "
        "   SET approval_scope = COALESCE(approval_scope, '{}'::jsonb) || '{\"used\": 1}'::jsonb, "
        "       expires_at = now(), updated_at = now() "
        " WHERE id = $1::uuid AND decision = 'approved'",
        request_id,
    )


async def apply_decision(
    *,
    slot_id: str,
    approved: bool,
    tenant_id: str,
    decided_by: str,
    request_id: str = "",
) -> dict[str, Any]:
    """CEO 결정을 슬롯에 적용한다. 어떤 경우에도 슬롯은 이 호출로 폐기된다."""
    slot = _find_by_id(slot_id) if slot_id else None
    if slot is None:
        return {"status": "expired" if approved else "discarded"}
    if slot.tenant_id != tenant_id:
        return {"status": "error", "error": "tenant_mismatch"}
    origin, username, password = slot.origin, slot.username, slot.password
    host, masked = host_of(origin), mask_username(username)
    discard(slot.id)
    base = {"origin": origin, "host": host, "username_masked": masked}
    if not approved:
        return {"status": "discarded", **base}
    try:
        from app.services.agent_vault_service import (
            find_agent_credential_by_username,
            update_agent_credential,
            upsert_agent_credential,
        )

        metadata = {
            "source": "browser_autosave", "policy": "ask", "verification_status": "verified",
        }
        existing = await find_agent_credential_by_username(
            tenant_id=tenant_id, origin=origin, username=username,
        )
        if existing and hmac.compare_digest(
            str(existing.get("password") or "").encode("utf-8"), password.encode("utf-8"),
        ):
            return {"status": "already_saved", **base}
        if existing:
            saved = await update_agent_credential(
                tenant_id=tenant_id, user_id=decided_by, credential_id=existing["id"],
                work_key=existing["work_key"], origin=origin, label=existing["label"],
                username=username, password=password,
                metadata={**(existing.get("metadata") or {}), **metadata},
            )
            status = "updated"
        else:
            saved = await upsert_agent_credential(
                tenant_id=tenant_id, user_id=decided_by, work_key=VAULT_WORK_KEY,
                origin=origin, label=f"{host} - {username}", username=username,
                password=password, metadata=metadata,
            )
            status = "saved"
        if request_id:
            try:
                await _consume_card(request_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser_autosave_consume_failed: %s", type(exc).__name__)
        return {"status": status, "credential_id": (saved or {}).get("id", ""), **base}
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_autosave_save_failed: %s", type(exc).__name__)
        return {"status": "error", "error": type(exc).__name__, **base}
