"""
AADS 인증 중앙 관리 — OAuth 토큰 로딩, 폴백, 클라이언트 생성.

모든 Claude LLM 호출은 이 모듈을 통해 토큰을 얻어야 함.
다른 파일에서 os.getenv("ANTHROPIC_API_KEY") 직접 사용 금지 (R-AUTH).

사용 예시:
    from app.core.auth_provider import get_oauth_tokens, get_base_url, create_anthropic_client

    tokens = get_oauth_tokens()           # [Primary, Fallback]
    client = create_anthropic_client()     # AsyncAnthropic(primary token)
    ok = has_valid_token()                 # True if any token exists
"""
from __future__ import annotations

import copy
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# ── 토큰 로딩 (모듈 초기화 시 1회) ──────────────────────────────────
_TOKEN_PRIMARY = os.getenv("ANTHROPIC_AUTH_TOKEN", "") or os.getenv("ANTHROPIC_API_KEY", "")
_TOKEN_FALLBACK = os.getenv("ANTHROPIC_AUTH_TOKEN_2", "") or os.getenv("ANTHROPIC_API_KEY_FALLBACK", "")
_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
_LITELLM_URL = os.getenv("LITELLM_BASE_URL", "http://aads-litellm:4000")
_LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")
_CLAUDE_RELAY_URL = os.getenv("CLAUDE_RELAY_URL", "http://host.docker.internal:8199")
_ENV_OAUTH_PATHS = (
    Path("/root/.genspark/.env.oauth"),
    Path("/app/.env.oauth"),
)

# 라벨 매핑 (토큰 prefix 20자 기준)
_KEY_LABELS: Dict[str, str] = {}
if _TOKEN_PRIMARY:
    _KEY_LABELS[_TOKEN_PRIMARY[:20]] = "moong76@gmail"
if _TOKEN_FALLBACK:
    _KEY_LABELS[_TOKEN_FALLBACK[:20]] = "moongoby@gmail"

# 런타임 순서 변경 가능한 리스트
_ordered_tokens: List[str] = [k for k in [_TOKEN_PRIMARY, _TOKEN_FALLBACK] if k]
_runtime_key_records: List[Dict[str, str]] = []


def _read_env_oauth_slot_tokens() -> dict:
    slot_tokens: Dict[str, str] = {}
    slot_labels: Dict[str, str] = {}
    current_slot = "1"
    for path in _ENV_OAUTH_PATHS:
        try:
            text = path.read_text()
        except Exception:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("CURRENT_OAUTH="):
                current_slot = line.split("=", 1)[1].strip() or current_slot
                continue
            if line.startswith("OAUTH_TOKEN_1="):
                slot_tokens["1"] = line.split("=", 1)[1].strip()
                continue
            if line.startswith("OAUTH_TOKEN_2="):
                slot_tokens["2"] = line.split("=", 1)[1].strip()
                continue
            if line.startswith("#"):
                lower = line.lower()
                for slot in ("1", "2"):
                    token_marker = "token{}:".format(slot)
                    if token_marker in lower:
                        label = line.split(":", 1)[1].strip().split("(")[0].strip()
                        slot_labels[slot] = label or "slot{}".format(slot)
        break
    return {"tokens": slot_tokens, "labels": slot_labels, "current_slot": current_slot}


def _build_env_records() -> List[Dict[str, str]]:
    slot_info = _read_env_oauth_slot_tokens()
    slot_tokens = slot_info["tokens"]
    slot_labels = slot_info["labels"]
    records: List[Dict[str, str]] = []
    for slot, token in (("1", _TOKEN_PRIMARY), ("2", _TOKEN_FALLBACK)):
        if not token:
            continue
        prefix = token[:20]
        label = slot_labels.get(slot) or _KEY_LABELS.get(prefix) or "slot{}".format(slot)
        records.append(
            {
                "id": slot,
                "key_name": "env_oauth_{}".format(slot),
                "label": label,
                "prefix": token[:12] + "...",
                "priority": len(records) + 1,
                "slot": slot,
                "source": "env",
                "value": token,
                "rate_limited_until": None,
            }
        )
        _KEY_LABELS[prefix] = label
        if slot not in slot_tokens:
            slot_tokens[slot] = token
    return records


def _assign_slots(records: List[Dict[str, str]]) -> List[Dict[str, str]]:
    slot_info = _read_env_oauth_slot_tokens()
    slot_tokens = slot_info["tokens"]
    if "1" not in slot_tokens and _TOKEN_PRIMARY:
        slot_tokens["1"] = _TOKEN_PRIMARY
    if "2" not in slot_tokens and _TOKEN_FALLBACK:
        slot_tokens["2"] = _TOKEN_FALLBACK
    assigned: List[Dict[str, str]] = []
    used_slots = set()
    for record in records:
        item = dict(record)
        value = item.get("value", "") or ""
        slot = ""
        for candidate_slot, candidate_token in slot_tokens.items():
            if not candidate_token:
                continue
            if value == candidate_token or value[:20] == candidate_token[:20]:
                slot = candidate_slot
                break
        if not slot:
            for candidate_slot in ("1", "2"):
                if candidate_slot not in used_slots:
                    slot = candidate_slot
                    break
        if slot:
            used_slots.add(slot)
        item["slot"] = slot
        if value:
            _KEY_LABELS[value[:20]] = item.get("label") or item.get("key_name") or "Unknown"
        assigned.append(item)
    return assigned


def _rebuild_runtime_state(records: List[Dict[str, str]]) -> None:
    global _ordered_tokens, _runtime_key_records
    _runtime_key_records = []
    ordered_tokens: List[str] = []
    for record in records:
        value = record.get("value", "") or ""
        if not value:
            continue
        item = dict(record)
        item["prefix"] = value[:12] + "..."
        _runtime_key_records.append(item)
        ordered_tokens.append(value)
    _ordered_tokens = ordered_tokens


async def _sync_relay_current_slot(slot: str) -> None:
    if slot not in ("1", "2"):
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            await client.post("{}/oauth/switch".format(_CLAUDE_RELAY_URL), json={"slot": slot})
    except Exception as e:
        logger.warning("auth_provider: relay slot sync failed: %s", e)


async def get_oauth_key_records_async(include_rate_limited: bool = True) -> List[Dict[str, str]]:
    """DB 기준 Anthropc OAuth 키 메타데이터. DB 실패 시 env 폴백."""
    try:
        from app.core.llm_key_provider import get_provider_key_records

        records = await get_provider_key_records("anthropic", include_rate_limited=include_rate_limited)
        if records:
            assigned = _assign_slots(records)
            _rebuild_runtime_state(assigned)
            return copy.deepcopy(assigned)
    except Exception as e:
        logger.warning("auth_provider: db oauth records fallback to env: %s", e)

    env_records = _build_env_records()
    _rebuild_runtime_state(env_records)
    return copy.deepcopy(env_records)


# ── 공개 API ────────────────────────────────────────────────────────

def get_oauth_tokens() -> List[str]:
    """[Primary, Fallback] 순서로 유효한 토큰 반환. 빈 토큰 제외."""
    return list(_ordered_tokens)


async def get_oauth_tokens_async() -> List[str]:
    """DB 우선, .env 폴백으로 Anthropic OAuth 토큰 반환."""
    records = await get_oauth_key_records_async(include_rate_limited=False)
    return [record.get("value", "") for record in records if record.get("value")]


def get_primary_token() -> str:
    """1순위 토큰 반환. 없으면 빈 문자열."""
    return _ordered_tokens[0] if _ordered_tokens else ""


def get_fallback_token() -> str:
    """2순위 토큰 반환. 없으면 빈 문자열."""
    return _ordered_tokens[1] if len(_ordered_tokens) > 1 else ""


def get_base_url() -> str:
    """Anthropic API base URL."""
    return _BASE_URL


def get_litellm_config() -> Dict[str, str]:
    """LiteLLM 프록시 설정 반환."""
    return {"url": _LITELLM_URL, "key": _LITELLM_KEY}


def get_token_labels() -> List[Dict[str, str]]:
    """프론트 표시용 토큰 정보. [{'label': 'moong76@gmail', 'prefix': 'sk-ant-oat01-3BC...'}]"""
    result = []
    records = _runtime_key_records or _build_env_records()
    for record in records:
        value = record.get("value", "") or ""
        result.append(
            {
                "label": record.get("label", "Unknown"),
                "prefix": record.get("prefix") or (value[:12] + "..." if value else ""),
                "key_name": record.get("key_name", ""),
                "priority": record.get("priority", 0),
                "slot": record.get("slot", ""),
            }
        )
    return result


def set_token_order(primary: str) -> bool:
    """레거시 동기 API: 메모리 내 순서만 변경."""
    global _ordered_tokens
    records = _runtime_key_records or _build_env_records()
    selected = None
    wanted = (primary or "").strip().lower()
    for record in records:
        if wanted in {
            str(record.get("slot", "")).lower(),
            str(record.get("key_name", "")).lower(),
            str(record.get("label", "")).lower(),
        }:
            selected = record
            break
    if selected:
        reordered = [selected] + [record for record in records if record is not selected]
        _rebuild_runtime_state(reordered)
        logger.info("auth_provider: runtime token order switched to %s", selected.get("label"))
        return True
    return False


async def set_token_order_async(primary: str) -> bool:
    """DB priority를 기준으로 Anthropic 키 순서를 영구 변경."""
    wanted = (primary or "").strip().lower()
    if not wanted:
        return False

    from app.core.db_pool import get_pool
    from app.core.llm_key_provider import invalidate_key_cache

    records = await get_oauth_key_records_async(include_rate_limited=True)
    if not records:
        return False

    selected = None
    for record in records:
        label = str(record.get("label", "")).strip().lower()
        key_name = str(record.get("key_name", "")).strip().lower()
        slot = str(record.get("slot", "")).strip().lower()
        aliases = {label, key_name, slot}
        if slot == "1":
            aliases.update({"gmail", "primary", "slot1"})
        elif slot == "2":
            aliases.update({"naver", "fallback", "slot2"})
        if wanted in aliases or wanted in label:
            selected = record
            break

    if not selected:
        return False

    reordered = [selected] + [record for record in records if record["key_name"] != selected["key_name"]]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for index, record in enumerate(reordered, start=1):
                await conn.execute(
                    """
                    UPDATE llm_api_keys
                    SET priority = $2,
                        updated_at = NOW()
                    WHERE key_name = $1
                    """,
                    record["key_name"],
                    index,
                )

    invalidate_key_cache()
    refreshed = await get_oauth_key_records_async(include_rate_limited=True)
    preferred_slot = refreshed[0].get("slot", "") if refreshed else ""
    if preferred_slot in ("1", "2"):
        await _sync_relay_current_slot(preferred_slot)
    logger.info(
        "auth_provider: db token order switched to %s (slot=%s)",
        selected.get("label"),
        selected.get("slot"),
    )
    return True


def rotate_oauth_primary_fallback() -> bool:
    """한도·크레딧류 API 오류 시 1순위↔2순위 OAuth 토큰 순서 교환 (런타임)."""
    global _ordered_tokens
    if len(_ordered_tokens) < 2:
        return False
    _ordered_tokens = [_ordered_tokens[1], _ordered_tokens[0]]
    logger.warning("auth_provider: primary/fallback rotated (quota or limit-class error)")
    return True


# -- Rate Limit Cooldown Tracking --
_token_cooldowns = {}  # {token_prefix_20: expire_timestamp}

def _parse_rl_reset(headers=None):
    if not headers:
        return None
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if ra:
        try: return time.time() + float(ra)
        except: pass
    rr = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if rr:
        try: return float(rr)
        except: pass
    return None

def mark_token_rate_limited(token, headers=None):
    global _ordered_tokens
    expire = _parse_rl_reset(headers) or (time.time() + 3600)
    _token_cooldowns[token[:20]] = expire
    logger.warning("token_rate_limited: prefix=%s until=%s", token[:12], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expire)))
    if token in _ordered_tokens and len(_ordered_tokens) > 1:
        _ordered_tokens = [t for t in _ordered_tokens if t != token] + [token]
        logger.warning("token_order_rotated: rate-limited token moved to end")
    return expire

def is_token_rate_limited(token):
    expire = _token_cooldowns.get(token[:20], 0)
    if time.time() >= expire:
        _token_cooldowns.pop(token[:20], None)
        return False
    return True

def get_available_tokens():
    available = [t for t in _ordered_tokens if not is_token_rate_limited(t)]
    return available if available else list(_ordered_tokens)



def create_anthropic_client(token: Optional[str] = None) -> AsyncAnthropic:
    """AsyncAnthropic 클라이언트 생성. token 미지정 시 primary 사용."""
    key = token or get_primary_token()
    return AsyncAnthropic(api_key=key, base_url=_BASE_URL)


def has_valid_token() -> bool:
    """유효한 OAuth 토큰이 하나 이상 있는지."""
    return bool(_ordered_tokens)


_rebuild_runtime_state(_build_env_records())
