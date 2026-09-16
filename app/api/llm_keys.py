"""LLM API 키 관리 API — llm_api_keys 테이블 CRUD + 레지스트리 동기화."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import require_internal_admin
from app.core.credential_vault import decrypt_value, encrypt_value
from app.core.db_pool import get_pool
from app.core.llm_key_provider import invalidate_key_cache
from app.services.model_registry import append_key_audit_log, invalidate_registry_cache, normalize_provider, sync_model_registry

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/llm-keys",
    tags=["llm-keys"],
    dependencies=[Depends(require_internal_admin)],
)


class LlmKeyCreate(BaseModel):
    provider: str
    key_name: str
    value: str
    label: str = ""
    priority: int = Field(1, ge=1)
    notes: str = ""
    is_active: bool = True

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        normalized = normalize_provider(value)
        if not normalized:
            raise ValueError("provider is required")
        return normalized

    @field_validator("key_name")
    @classmethod
    def validate_key_name(cls, value: str) -> str:
        key_name = (value or "").strip()
        if not key_name:
            raise ValueError("key_name is required")
        return key_name

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("value is required")
        return value.strip()


class LlmKeyUpdate(BaseModel):
    value: str | None = None
    label: str | None = None
    priority: int | None = Field(None, ge=1)
    is_active: bool | None = None
    notes: str | None = None
    provider: str | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_provider(value)
        if not normalized:
            raise ValueError("provider is required")
        return normalized


def _mask(val: str) -> str:
    if len(val) <= 8:
        return "****"
    return val[:4] + "****" + val[-4:]


async def _validate_priority(
    conn: asyncpg.Connection,
    *,
    provider: str,
    priority: int,
    is_active: bool,
    key_id: int | None = None,
) -> None:
    if not is_active:
        return
    row = await conn.fetchrow(
        """
        SELECT id, key_name
        FROM llm_api_keys
        WHERE provider = $1
          AND priority = $2
          AND is_active = TRUE
          AND ($3::int IS NULL OR id <> $3)
        LIMIT 1
        """,
        provider,
        priority,
        key_id,
    )
    if row:
        raise HTTPException(
            status_code=409,
            detail=f"Active priority conflict: {provider} priority {priority} already used by {row['key_name']}",
        )


async def _run_registry_sync(reason: str) -> dict[str, Any] | None:
    invalidate_key_cache()
    invalidate_registry_cache()
    try:
        return await sync_model_registry(triggered_by="llm_keys_api", reason=reason)
    except Exception:
        logger.exception("llm_keys.registry_sync_failed", extra={"reason": reason})
        return {"ok": False, "error": "sync_failed"}


@router.get("")
async def list_llm_keys() -> list[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, provider, key_name, encrypted_value, label, priority,
                   is_active, rate_limited_until, last_used_at, last_verified_at,
                   created_at, updated_at, notes
            FROM llm_api_keys
            ORDER BY provider, priority, id
            """
        )
    result = []
    for row in rows:
        try:
            plain = decrypt_value(row["encrypted_value"])
            masked = _mask(plain)
        except Exception:
            masked = "****"
        result.append(
            {
                "id": row["id"],
                "provider": normalize_provider(row["provider"]),
                "key_name": row["key_name"],
                "masked_value": masked,
                "label": row["label"],
                "priority": row["priority"],
                "is_active": row["is_active"],
                "rate_limited_until": row["rate_limited_until"].isoformat() if row["rate_limited_until"] else None,
                "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
                "last_verified_at": row["last_verified_at"].isoformat() if row["last_verified_at"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "notes": row["notes"],
            }
        )
    return result


@router.get("/codex-usage")
async def codex_usage() -> dict[str, Any]:
    """코덱스 계정별 사용량. 대시보드 '코덱스 사용량' 카드의 데이터원이다.

    ChatGPT 구독 인증이라 API 청구 대시보드가 없고 CLI 에도 usage 서브커맨드가
    없다. 사용량 원본은 호스트의 rollout 파일이고, scripts/codex_usage.py 가
    10분마다 긁어 codex_usage_snapshots 에 올린다. 여기서는 그 표만 읽는다.
    설계: aads-docs/docs/PRD-LLM-ACCOUNT-RUNTIME-BINDING-v1.0.md
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT k.key_name, k.label, k.priority, k.is_active, k.rate_limited_until,
                   s.used_percent, s.window_minutes, s.resets_at, s.snapshot_at,
                   s.ok_72h, s.limit_72h, s.sessions, s.collected_at
            FROM llm_api_keys k
            LEFT JOIN codex_usage_snapshots s ON s.key_name = k.key_name
            WHERE k.provider = 'codex'
            ORDER BY k.priority, k.id
            """
        )

    now = datetime.now(timezone.utc)
    accounts: list[dict[str, Any]] = []
    for row in rows:
        limited_until = row["rate_limited_until"]
        limited = bool(limited_until and limited_until > now)
        # 스냅샷이 얼마나 낡았는지는 반드시 같이 준다. 한도에 걸린 호출은 사용률을
        # 갱신해주지 않아, 숫자만 보면 남아 있는 것처럼 오해한다.
        age_hours = round((now - row["snapshot_at"]).total_seconds() / 3600, 1) if row["snapshot_at"] else None
        accounts.append(
            {
                "key_name": row["key_name"],
                "label": row["label"],
                "priority": row["priority"],
                "is_active": row["is_active"],
                "rate_limited": limited,
                "rate_limited_until": limited_until.isoformat() if limited_until else None,
                "used_percent": float(row["used_percent"]) if row["used_percent"] is not None else None,
                "window_minutes": row["window_minutes"],
                "resets_at": row["resets_at"].isoformat() if row["resets_at"] else None,
                "snapshot_age_hours": age_hours,
                "ok_72h": row["ok_72h"] or 0,
                "limit_72h": row["limit_72h"] or 0,
                "sessions": row["sessions"] or 0,
                "collected_at": row["collected_at"].isoformat() if row["collected_at"] else None,
                "has_snapshot": row["collected_at"] is not None,
            }
        )

    usable = [a for a in accounts if a["is_active"] and not a["rate_limited"]]
    return {
        "accounts": accounts,
        "usable_count": len(usable),
        "total_count": len(accounts),
        "collected_at": max(
            (a["collected_at"] for a in accounts if a["collected_at"]), default=None
        ),
    }


async def _relay_call(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """릴레이(호스트 프로세스)로 넘긴다.

    자격증명 파일은 호스트에 있고 이 컨테이너에는 마운트돼 있지 않다. CLI 를
    띄울 수 있는 것은 호스트에서 도는 릴레이뿐이라, 재로그인은 전부 프록시다.
    경로/시크릿 규약은 app/api/ops.py 의 codex-usage 프록시와 같게 맞춘다.
    """
    import httpx

    from app.api.ops import _CLAUDE_RELAY_URL, _load_relay_secret

    headers: dict[str, str] = {}
    secret = _load_relay_secret()
    if secret:
        headers["X-Claude-Relay-Secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=2.0)) as client:
            resp = await client.request(method, f"{_CLAUDE_RELAY_URL}{path}",
                                        headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"릴레이에 닿지 못했다: {str(exc)[:200]}") from exc
    try:
        body = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="릴레이 응답을 읽을 수 없다") from None
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=body.get("error") or "로그인 요청 실패")
    return body


class AccountLoginStart(BaseModel):
    # 'codex:<KEY_NAME>' 또는 'claude:<slot 번호>'
    target: str


class AccountLoginCode(BaseModel):
    code: str


# 구독 OAuth 로 붙는 provider. 이쪽은 키를 붙여넣어 등록할 수 없다 — access 토큰만
# 넣으면 약 8시간 뒤 401 로 죽는다(2026-09-15 ANTHROPIC_AUTH_TOKEN_3 사고).
SUBSCRIPTION_PROVIDERS = ("anthropic", "codex")

# 드롭다운에 정식 이름으로만 노출할 provider. 별칭(claude/google/dashscope…)은 감춘다.
CANONICAL_PROVIDERS = (
    "anthropic", "codex", "openai", "gemini", "groq", "deepseek", "openrouter",
    "qwen", "kimi", "minimax", "litellm", "cerebras", "together", "mistral",
    "nvidia", "sambanova", "huggingface", "tavily", "kling",
)


def _fingerprint(value: str) -> str:
    """값 비교용 지문. 평문은 어디에도 남기지 않는다 (R-KEY)."""
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _usage_windows(provider: str, row: Any, slot_row: dict[str, Any] | None) -> list[dict[str, Any]]:
    """계정의 한도 창을 창 길이와 함께 돌려준다.

    두 provider 의 창 구성이 다르다 — 코덱스는 주간(10080분) 하나, 클로드는
    5시간(300분)과 주간을 같이 쓴다. 화면이 'primary/secondary' 라는 이름으로
    추측하면 안 된다. 코덱스의 primary 는 주간이고 클로드의 primary 는 5시간이라,
    같은 이름이 다른 창을 가리킨다. 그래서 window_minutes 를 붙여 내려준다.
    """
    out: list[dict[str, Any]] = []
    if provider == "anthropic":
        for key in ("primary", "secondary"):
            win = (slot_row or {}).get(key) or {}
            if win.get("used_percent") is None:
                continue
            out.append({
                "window_minutes": win.get("window_minutes"),
                "used_percent": float(win["used_percent"]),
                "resets_at": win.get("resets_at"),
            })
        return out

    if row["used_percent"] is not None:
        out.append({
            "window_minutes": 10080,
            "used_percent": float(row["used_percent"]),
            "resets_at": row["resets_at"].isoformat() if row["resets_at"] else None,
        })
    return out


def _account_state(row: Any, binding: dict[str, Any] | None, now: datetime) -> str:
    """행 하나의 상태를 서버가 정한다. 화면은 색만 칠한다."""
    if not row["is_active"]:
        return "inactive"
    if binding is None:
        return "unknown"          # 릴레이 무응답 — 등록 정보는 살리고 상태만 비운다
    if binding.get("needs_login"):
        return "needs_login"
    limited = row["rate_limited_until"]
    if limited and limited > now:
        return "rate_limited"
    return "ok"


@router.get("/overview")
async def llm_overview() -> dict[str, Any]:
    """설정 화면 '통합 계정 카드' 의 단일 데이터원.

    화면이 /llm-keys · /codex-usage · /account-bindings 를 따로 부르고 프론트에서
    키 이름으로 이어 붙이던 것을 서버로 옮긴다. 2026-09-16 오전, 그렇게 조인하던
    수집기가 계정을 갈아탄 세션의 이전 기록을 새 계정에 귀속시켜 멀쩡한 계정을
    껐다. 합치는 곳이 두 군데면 같은 실수가 두 번 난다.
    설계: aads-docs/docs/PRD-SETTINGS-UNIFIED-ACCOUNT-CARD-v1.0.md
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT k.id, k.provider, k.key_name, k.encrypted_value, k.label, k.priority,
                   k.is_active, k.rate_limited_until, k.notes,
                   s.used_percent, s.resets_at, s.snapshot_at,
                   s.ok_72h, s.limit_72h, s.collected_at
            FROM llm_api_keys k
            LEFT JOIN codex_usage_snapshots s ON s.key_name = k.key_name
            ORDER BY k.provider, k.priority, k.id
            """
        )

    # 바인딩은 호스트의 릴레이만 안다. 실패해도 화면 전체를 죽이지 않는다.
    bindings: dict[str, dict[str, Any]] | None
    try:
        payload = await _relay_call("GET", "/account-bindings")
        bindings = {}
        for b in payload.get("bindings", []):
            target = str(b.get("target", ""))
            kind, _, name = target.partition(":")
            key = name if kind == "codex" else f"slot{name}"
            bindings[key] = b
    except HTTPException:
        bindings = None
        logger.warning("llm_keys.overview.bindings_unavailable")

    # 클로드는 5시간 창과 주간 창을 따로 쓴다. 코덱스는 주간 하나뿐이다.
    # 슬롯별 스냅샷은 oauth_usage_tracker 가 이미 모으고 있으므로 가져다 쓴다.
    slot_usage: dict[str, dict[str, Any]] = {}
    try:
        from app.services.oauth_usage_tracker import get_slot_usage_all

        for row in await get_slot_usage_all():
            slot_usage[f"slot{row.get('slot')}"] = row
    except Exception:
        logger.warning("llm_keys.overview.slot_usage_unavailable")

    # anthropic 키 ↔ 슬롯 매핑. 릴레이 슬롯 규약과 같은 순서다(priority 오름차순).
    slot_of: dict[str, str] = {}
    anthropic_rows = [r for r in rows if normalize_provider(r["provider"]) == "anthropic"]
    for idx, r in enumerate(sorted(anthropic_rows, key=lambda x: (x["priority"], x["id"])), start=1):
        slot_of[r["key_name"]] = f"slot{idx}"

    now = datetime.now(timezone.utc)
    accounts: list[dict[str, Any]] = []
    providers: dict[str, dict[str, Any]] = {}

    for row in rows:
        provider = normalize_provider(row["provider"])
        try:
            masked = _mask(decrypt_value(row["encrypted_value"]))
        except Exception:
            masked = "****"

        if provider in SUBSCRIPTION_PROVIDERS:
            slot = slot_of.get(row["key_name"])
            bkey = slot if provider == "anthropic" else row["key_name"]
            binding = bindings.get(bkey) if bindings is not None else None
            accounts.append({
                "key_name": row["key_name"], "provider": provider, "label": row["label"],
                "priority": row["priority"], "is_active": row["is_active"],
                "kind": "subscription", "masked_value": masked,
                "slot": slot,
                "state": _account_state(row, binding, now),
                "bound": bool(binding and binding.get("bound")),
                "needs_login": bool(binding and binding.get("needs_login")),
                "login_in_progress": bool(binding and binding.get("login_in_progress")),
                # 릴레이 규약은 'claude:<슬롯번호>' 와 'codex:<KEY_NAME>' 이다.
                # provider 이름(anthropic)을 그대로 쓰면 resolve_target 이 거절한다.
                "login_target": (f"claude:{slot[4:]}" if provider == "anthropic" and slot else None)
                                or (f"codex:{row['key_name']}" if provider == "codex" else None),
                "subscription": (binding or {}).get("subscription"),
                "rate_limited_until": row["rate_limited_until"].isoformat() if row["rate_limited_until"] else None,
                "windows": _usage_windows(provider, row, slot_usage.get(slot or "")),
                "used_percent": float(row["used_percent"]) if row["used_percent"] is not None else None,
                "resets_at": row["resets_at"].isoformat() if row["resets_at"] else None,
                "snapshot_age_hours": (round((now - row["snapshot_at"]).total_seconds() / 3600, 1)
                                       if row["snapshot_at"] else None),
                "ok_72h": row["ok_72h"] or 0, "limit_72h": row["limit_72h"] or 0,
                "notes": row["notes"],
            })
        else:
            p = providers.setdefault(provider, {"provider": provider, "key_count": 0, "usable": 0, "keys": []})
            limited = bool(row["rate_limited_until"] and row["rate_limited_until"] > now)
            state = "inactive" if not row["is_active"] else ("rate_limited" if limited else "ok")
            p["key_count"] += 1
            p["usable"] += 1 if state == "ok" else 0
            p["keys"].append({
                "id": row["id"], "key_name": row["key_name"], "label": row["label"],
                "priority": row["priority"], "masked_value": masked,
                "is_active": row["is_active"], "state": state, "notes": row["notes"],
            })

    def usable(kind: str) -> dict[str, int]:
        sel = [a for a in accounts if a["provider"] == kind]
        return {"usable": sum(1 for a in sel if a["state"] == "ok"), "total": len(sel)}

    collected = [row["collected_at"] for row in rows if row["collected_at"]]
    return {
        "summary": {
            "action_required": sum(1 for a in accounts if a["state"] in ("needs_login", "rate_limited")),
            "codex": usable("codex"),
            "anthropic": usable("anthropic"),
            "subscription_total": len(accounts),
            "apikey_total": sum(p["key_count"] for p in providers.values()),
            "total": len(rows),
            "bindings_available": bindings is not None,
            "collected_at": max(collected).isoformat() if collected else None,
        },
        "accounts": accounts,
        "providers": sorted(providers.values(), key=lambda p: (-p["key_count"], p["provider"])),
    }


@router.get("/providers")
async def provider_options() -> dict[str, Any]:
    """키 추가 드롭다운 목록. 쓰는 것 먼저, 안 쓰는 정식 provider 다음."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT provider, count(*) AS n FROM llm_api_keys WHERE is_active "
            "GROUP BY provider ORDER BY count(*) DESC, provider"
        )
    used = [{"provider": normalize_provider(r["provider"]), "key_count": r["n"],
             "kind": "subscription" if normalize_provider(r["provider"]) in SUBSCRIPTION_PROVIDERS else "apikey"}
            for r in rows]
    used_names = {u["provider"] for u in used}
    unused = [{"provider": p, "key_count": 0,
               "kind": "subscription" if p in SUBSCRIPTION_PROVIDERS else "apikey"}
              for p in CANONICAL_PROVIDERS if p not in used_names]
    return {"used": used, "unused": unused}


@router.get("/new-key-defaults")
async def new_key_defaults(provider: str) -> dict[str, Any]:
    """provider 를 고르면 나머지를 서버가 채워준다.

    키 이름 규칙은 provider 마다 다르다 — ANTHROPIC_AUTH_TOKEN_3,
    CODEX_OAUTH_JINAH, GEMINI_API_KEY_JINAH2. 하드코딩하지 않고 그 provider 의
    기존 이름에서 공통 접두를 뽑아 다음 번호를 붙인다.
    """
    provider = normalize_provider(provider)
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key_name, priority FROM llm_api_keys "
            "WHERE provider = $1 AND is_active ORDER BY priority, id",
            provider,
        )

    names = [r["key_name"] for r in rows]
    used_priorities = sorted({r["priority"] for r in rows})
    next_priority = next((n for n in range(1, len(used_priorities) + 2) if n not in used_priorities), 1)

    if names:
        # 공통 접두 = 기존 이름들의 최장 공통 앞부분. 끝의 구분자·숫자는 떼어낸다.
        prefix = names[0]
        for n in names[1:]:
            i = 0
            while i < min(len(prefix), len(n)) and prefix[i] == n[i]:
                i += 1
            prefix = prefix[:i]
        prefix = prefix.rstrip("_0123456789") or f"{provider.upper()}_API_KEY"
    else:
        prefix = f"{provider.upper()}_API_KEY"

    # 기존 키가 하나라도 있으면 반드시 접미를 붙인다. 접두만 쓰면 codex 처럼
    # 이름이 MAIN/JINAH 로 갈린 provider 에서 'CODEX_OAUTH' 같은 맨 이름이 나온다.
    existing = set(names)
    if not names:
        suggested = prefix
    else:
        n = 2
        while f"{prefix}_{n}" in existing:
            n += 1
        suggested = f"{prefix}_{n}"

    kind = "subscription" if provider in SUBSCRIPTION_PROVIDERS else "apikey"
    login_targets: list[dict[str, Any]] = []
    if kind == "subscription":
        try:
            payload = await _relay_call("GET", "/account-bindings")
            want = "claude" if provider == "anthropic" else "codex"
            login_targets = [
                {"target": b["target"], "account": b["account"], "needs_login": b["needs_login"]}
                for b in payload.get("bindings", [])
                if str(b.get("target", "")).startswith(want + ":")
            ]
        except HTTPException:
            login_targets = []

    return {
        "provider": provider,
        "kind": kind,
        "suggested_key_name": suggested,
        "used_priorities": used_priorities,
        "next_priority": next_priority,
        "existing_count": len(names),
        "login_targets": login_targets,
    }


class DuplicateCheck(BaseModel):
    value: str


@router.post("/check-duplicate")
async def check_duplicate(body: DuplicateCheck) -> dict[str, Any]:
    """같은 값이 이미 등록돼 있는지. 지문으로만 비교한다 (R-KEY)."""
    target = _fingerprint(body.value.strip())
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key_name, provider, encrypted_value FROM llm_api_keys")
    for row in rows:
        try:
            if _fingerprint(decrypt_value(row["encrypted_value"])) == target:
                return {"duplicate": True, "key_name": row["key_name"],
                        "provider": normalize_provider(row["provider"])}
        except Exception:
            continue
    return {"duplicate": False}


@router.get("/account-bindings")
async def account_bindings() -> dict[str, Any]:
    """구독 계정의 런타임 바인딩 상태. DB 등록과 별개로 파일 존재를 본다."""
    return await _relay_call("GET", "/account-bindings")


@router.post("/account-login")
async def start_account_login(body: AccountLoginStart) -> dict[str, Any]:
    """구독 계정 재로그인 시작. 화면의 '재로그인' 버튼이 부른다."""
    result = await _relay_call("POST", "/account-login", {"target": body.target})
    logger.info("llm_keys.account_login_start", extra={"target": body.target, "state": result.get("state")})
    return result


@router.get("/account-login/{login_id}")
async def get_account_login(login_id: str) -> dict[str, Any]:
    return await _relay_call("GET", f"/account-login/{login_id}")


@router.post("/account-login/{login_id}/code")
async def submit_account_login_code(login_id: str, body: AccountLoginCode) -> dict[str, Any]:
    """클로드 로그인은 stdin 으로 코드를 받는다 — 화면 입력값을 그대로 넘긴다."""
    return await _relay_call("POST", f"/account-login/{login_id}/code", {"code": body.code})


@router.delete("/account-login/{login_id}")
async def cancel_account_login(login_id: str) -> dict[str, Any]:
    return await _relay_call("DELETE", f"/account-login/{login_id}")


@router.post("")
async def create_llm_key(body: LlmKeyCreate) -> dict[str, Any]:
    encrypted = encrypt_value(body.value)
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _validate_priority(
                    conn,
                    provider=body.provider,
                    priority=body.priority,
                    is_active=body.is_active,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO llm_api_keys (
                        provider, key_name, encrypted_value, label, priority, notes, is_active,
                        rate_limited_until, last_verified_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, NULL, NOW())
                    RETURNING id, provider, key_name, label, priority, is_active, created_at
                    """,
                    body.provider,
                    body.key_name,
                    encrypted,
                    body.label,
                    body.priority,
                    body.notes,
                    body.is_active,
                )
                await append_key_audit_log(
                    conn,
                    key_id=row["id"] if row else None,
                    provider=body.provider,
                    key_name=body.key_name,
                    event_type="create",
                    actor="llm_keys_api",
                    details={
                        "label": body.label,
                        "priority": body.priority,
                        "is_active": body.is_active,
                        "notes_length": len(body.notes or ""),
                    },
                )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Duplicate key_name") from exc
    if not row:
        raise HTTPException(status_code=500, detail="Insert failed")
    sync_result = await _run_registry_sync(f"create:{body.key_name}")
    result = dict(row)
    if sync_result and not sync_result.get("ok", True):
        result["registry_sync"] = sync_result
    return result


@router.put("/{key_id}")
async def update_llm_key(key_id: int, body: LlmKeyUpdate) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT id, provider, key_name, label, priority, is_active, notes
                FROM llm_api_keys
                WHERE id = $1
                """,
                key_id,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Key not found")

            provider = body.provider or normalize_provider(existing["provider"])
            priority = body.priority if body.priority is not None else existing["priority"]
            is_active = body.is_active if body.is_active is not None else existing["is_active"]
            await _validate_priority(
                conn,
                provider=provider,
                priority=priority,
                is_active=is_active,
                key_id=key_id,
            )

            updates: list[str] = ["updated_at=NOW()"]
            params: list[Any] = []
            idx = 1

            if body.value is not None:
                if not body.value.strip():
                    raise HTTPException(status_code=400, detail="value cannot be empty")
                updates.append(f"encrypted_value=${idx}")
                params.append(encrypt_value(body.value.strip()))
                idx += 1
                updates.append("last_verified_at=NULL")
                updates.append("rate_limited_until=NULL")
            if body.label is not None:
                updates.append(f"label=${idx}")
                params.append(body.label)
                idx += 1
            if body.priority is not None:
                updates.append(f"priority=${idx}")
                params.append(body.priority)
                idx += 1
            if body.is_active is not None:
                updates.append(f"is_active=${idx}")
                params.append(body.is_active)
                idx += 1
            if body.notes is not None:
                updates.append(f"notes=${idx}")
                params.append(body.notes)
                idx += 1
            if body.provider is not None:
                updates.append(f"provider=${idx}")
                params.append(body.provider)
                idx += 1

            params.append(key_id)
            row = await conn.fetchrow(
                f"""
                UPDATE llm_api_keys
                SET {', '.join(updates)}
                WHERE id=${idx}
                RETURNING id, provider, key_name, label, priority, is_active, updated_at
                """,
                *params,
            )
            await append_key_audit_log(
                conn,
                key_id=key_id,
                provider=provider,
                key_name=existing["key_name"],
                event_type="update",
                actor="llm_keys_api",
                details={
                    "changed_fields": sorted(body.model_dump(exclude_none=True).keys()),
                    "priority": priority,
                    "is_active": is_active,
                },
            )

    invalidate_key_cache(existing["key_name"])
    sync_result = await _run_registry_sync(f"update:{existing['key_name']}")
    result = dict(row)
    if sync_result and not sync_result.get("ok", True):
        result["registry_sync"] = sync_result
    return result


@router.post("/{key_id}/activate")
async def activate_llm_key(key_id: int) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, provider, key_name, priority FROM llm_api_keys WHERE id=$1",
                key_id,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Key not found")
            provider = normalize_provider(existing["provider"])
            await _validate_priority(
                conn,
                provider=provider,
                priority=existing["priority"],
                is_active=True,
                key_id=key_id,
            )
            row = await conn.fetchrow(
                """
                UPDATE llm_api_keys
                SET is_active = TRUE,
                    rate_limited_until = NULL,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id, provider, key_name, is_active, updated_at
                """,
                key_id,
            )
            await append_key_audit_log(
                conn,
                key_id=key_id,
                provider=provider,
                key_name=existing["key_name"],
                event_type="activate",
                actor="llm_keys_api",
                details={"priority": existing["priority"]},
            )

    invalidate_key_cache(existing["key_name"])
    sync_result = await _run_registry_sync(f"activate:{existing['key_name']}")
    result = dict(row)
    if sync_result and not sync_result.get("ok", True):
        result["registry_sync"] = sync_result
    return result


@router.delete("/{key_id}")
async def deactivate_llm_key(key_id: int) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, provider, key_name FROM llm_api_keys WHERE id=$1",
                key_id,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Key not found")
            row = await conn.fetchrow(
                """
                UPDATE llm_api_keys
                SET is_active = FALSE, updated_at = NOW()
                WHERE id = $1
                RETURNING id, provider, key_name, is_active, updated_at
                """,
                key_id,
            )
            await append_key_audit_log(
                conn,
                key_id=key_id,
                provider=normalize_provider(existing["provider"]),
                key_name=existing["key_name"],
                event_type="deactivate",
                actor="llm_keys_api",
                details={},
            )

    invalidate_key_cache(existing["key_name"])
    sync_result = await _run_registry_sync(f"deactivate:{existing['key_name']}")
    result = dict(row)
    if sync_result and not sync_result.get("ok", True):
        result["registry_sync"] = sync_result
    return result
