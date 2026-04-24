"""
AADS-185: 모델 선택기 — Claude CLI Relay + LiteLLM 분기
- Claude 인텐트: Claude Code CLI 경유 (MCP 도구 브릿지, OAuth 토큰 자동 관리)
- Gemini 인텐트 (casual, greeting): LiteLLM 경유
- Gemini Direct (grounding, deep_research): gemini_search_service / gemini_research_service
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

import time as _time_mod
import contextvars
from datetime import datetime, timezone

_ctx_temperature: contextvars.ContextVar[float] = contextvars.ContextVar('_ctx_temperature', default=0.2)

import asyncpg
import httpx
from anthropic import AsyncAnthropic, APIStatusError, APIConnectionError, RateLimitError
from app.config import Settings
from app.core.llm_key_provider import get_api_key as _get_db_key, get_provider_keys as _get_provider_keys
from app.services.model_registry import get_executable_model_ids as _get_registry_executable_model_ids
from app.services.model_registry import list_registered_models as _list_registered_models
from app.services.intent_router import IntentResult

logger = logging.getLogger(__name__)

settings = Settings()

# LiteLLM 경유: 베이스 URL 단일화( _anthropic base_url == httpx URL ). 빈 env 문자열 방지.
_LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")
_LITELLM_BASE_RESOLVED = (os.getenv("LITELLM_BASE_URL") or "").strip() or "http://aads-litellm:4000"
_LITELLM_URL = _LITELLM_BASE_RESOLVED
LITELLM_BASE_URL = _LITELLM_BASE_RESOLVED

# 환경변수화: max_tokens 최고한도 (재배포 없이 .env로 조정 가능)
_MAX_TOKENS_CLAUDE = int(os.getenv("MAX_TOKENS_CLAUDE", "32768"))
_MAX_TOKENS_CLAUDE_THINKING = int(os.getenv("MAX_TOKENS_CLAUDE_THINKING", "128000"))
_MAX_TOKENS_GEMINI = int(os.getenv("MAX_TOKENS_GEMINI", "65536"))
_MAX_TOKENS_GEMINI_THINKING = int(os.getenv("MAX_TOKENS_GEMINI_THINKING", "65536"))

class _StripAuthTransport(httpx.AsyncBaseTransport):
    """SDK 자동 Authorization 헤더 제거 — LiteLLM x-api-key만 사용."""
    def __init__(self):
        self._inner = httpx.AsyncHTTPTransport()
    async def handle_async_request(self, request):
        raw = [(k, v) for k, v in request.headers.raw if k.lower() != b"authorization"]
        request.headers = httpx.Headers(raw)
        return await self._inner.handle_async_request(request)

def _get_anthropic_client() -> AsyncAnthropic:
    """LiteLLM 경유 Anthropic 클라이언트 반환."""
    return AsyncAnthropic(
        api_key=_LITELLM_API_KEY,
        base_url=_LITELLM_URL,
        http_client=httpx.AsyncClient(transport=_StripAuthTransport()),
        max_retries=5,
    )

def _quota_class_http_error(status: int, exc: BaseException) -> bool:
    """402/429/403 또는 본문·메시지에 limit(한도) 포함 시 OAuth 교대 대상."""
    if status in (402, 429, 403):
        return True
    parts = [str(exc).lower()]
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body).lower() if isinstance(body, (dict, list)) else str(body).lower())
        except Exception as e:
            logger.debug("json_dumps_failed_in_quota_check: %s", e)
            parts.append(str(body).lower())
    return "limit" in " ".join(parts)


def _switch_oat_token():
    """OAuth 순서 교환 후 Anthropic 직접 클라이언트로 전환(한도 회피). 직접 클라이언트 실패 시 순서 롤백."""
    global _anthropic
    from app.core.auth_provider import create_anthropic_client, rotate_oauth_primary_fallback

    if not rotate_oauth_primary_fallback():
        logger.warning("oat_token_switch: no second OAuth token — cannot rotate")
        return False
    try:
        _anthropic = create_anthropic_client()
        logger.warning("oat_token_switch: direct Anthropic client bound to new primary OAuth")
        return True
    except Exception as ex:
        logger.warning("oat_token_switch: direct client failed %s — rolling back token order", ex)
        rotate_oauth_primary_fallback()
        _anthropic = _get_anthropic_client()
        return False


_anthropic = _get_anthropic_client()

LITELLM_API_KEY = _LITELLM_API_KEY

# Claude CLI Relay (호스트에서 실행, Docker → host.docker.internal)
_CLAUDE_RELAY_URL = os.getenv("CLAUDE_RELAY_URL", "http://host.docker.internal:8199")
_CLAUDE_CLI_ENABLED = os.getenv("CLAUDE_CLI_ENABLED", "true").lower() == "true"
# 릴레이 oauth_slot: 1=Gmail, 2=Naver. 기본은 Naver 먼저 (false 로 Gmail 우선 복귀)
_CLAUDE_RELAY_NAVER_FIRST = os.getenv("CLAUDE_RELAY_NAVER_FIRST", "false").lower() in ("1", "true", "yes")

# 슬롯 쿨다운 (429/한도 오류 시 해당 슬롯 일시 건너뜀)
_SLOT_COOLDOWN: Dict[str, float] = {}  # {slot: expire_timestamp}
_COOLDOWN_SECS = 300  # 5분

def _parse_rl_reset_ms(headers=None):
    if not headers:
        return None
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if ra:
        try: return _time_mod.time() + float(ra)
        except (ValueError, TypeError): pass
    rr = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if rr:
        try: return float(rr)
        except (ValueError, TypeError): pass
    return None

def _mark_slot_cooldown(slot: str, headers=None, duration_override: int = None) -> None:
    """429/한도 오류 시 슬롯 쿨다운. 헤더 기반 해제 시각, 없으면 5분."""
    if duration_override:
        expire = _time_mod.time() + duration_override
    else:
        expire = _parse_rl_reset_ms(headers)
        if expire is None:
            expire = _time_mod.time() + _COOLDOWN_SECS
    _SLOT_COOLDOWN[slot] = expire
    until_str = _time_mod.strftime("%H:%M:%S", _time_mod.localtime(expire))
    logger.info("slot_cooldown_set: slot=%s until=%s", slot, until_str)

def _is_slot_available(slot: str) -> bool:
    """슬롯이 쿨다운 중이 아닌지 확인. 만료 시 자동 해제."""
    expire = _SLOT_COOLDOWN.get(slot, 0)
    if _time_mod.time() >= expire:
        _SLOT_COOLDOWN.pop(slot, None)
        return True
    return False

# Agent SDK OAuth 토큰 — auth_provider 경유 (R-AUTH)
from app.core.auth_provider import (
    get_oauth_tokens as _ap_get_tokens,
    get_oauth_key_records_async as _ap_get_key_records_async,
    get_token_labels as _ap_get_labels,
    set_token_order as _ap_set_order,
)
from app.core.llm_key_provider import mark_key_rate_limited as _mark_key_rate_limited
from app.services.oauth_usage_tracker import log_usage as _log_oauth_usage


def get_key_order() -> List[Dict[str, str]]:
    """현재 키 순서 반환 (프론트 표시용). auth_provider 위임."""
    return _ap_get_labels()


def set_key_order(primary: str) -> bool:
    """키 순서 변경. auth_provider 위임."""
    return _ap_set_order(primary)


async def _get_claude_slot_records() -> Dict[str, Dict[str, Any]]:
    """Anthropic DB priority를 relay slot 기준으로 재구성."""
    try:
        records = await _ap_get_key_records_async(include_rate_limited=True)
    except Exception as e:
        logger.warning("oauth_slot_records_failed: %s", e)
        records = []
    slot_records: Dict[str, Dict[str, Any]] = {}
    for record in records:
        slot = str(record.get("slot", "") or "")
        if slot in ("1", "2") and slot not in slot_records:
            slot_records[slot] = record
    return slot_records


def _is_db_slot_rate_limited(record: Optional[Dict[str, Any]]) -> bool:
    if not record:
        return False
    until = record.get("rate_limited_until")
    if not until:
        return False
    if isinstance(until, datetime):
        target = until
    else:
        try:
            target = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except Exception:
            return False
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return target > datetime.now(timezone.utc)

# AADS session_id → CLI session_id 매핑 (대화 이어가기용)
_cli_session_map: Dict[str, str] = {}  # {aads_session_id: cli_session_id}

_INTENT_POLICY_CACHE_TTL_SECONDS = 300
_INTENT_POLICY_CACHE: Dict[str, Any] = {"expires_at": 0.0, "policies": {}}
_INTENT_POLICY_MODEL_ALIASES = {
    "claude-sonnet-4-6": "claude-sonnet",
    "claude-sonnet-4-5": "claude-sonnet",
    "claude-haiku-4-5": "claude-haiku",
    "claude-haiku-4-5-20251001": "claude-haiku",
    "claude-opus-4-7": "claude-opus",
    "claude-opus-4-6": "claude-opus",
    "claude-opus-4-5": "claude-opus",
    "claude-opus-46": "claude-opus",
}
_INTENT_POLICY_CLAUDE_RANK = {"claude-haiku": 0, "claude-sonnet": 1, "claude-opus": 2}
_HAIKU_FALLBACK_INTENTS = {"greeting", "casual"}
_SONNET_INTENTS = {
    "search", "url_read", "browser", "task_query",
    "service_inspection", "code_explorer", "analyze_changes",
}


def _normalize_intent_policy_model(model: Any) -> str:
    model_name = str(model or "").strip()
    return _INTENT_POLICY_MODEL_ALIASES.get(model_name, model_name)


def invalidate_intent_policy_cache() -> None:
    _INTENT_POLICY_CACHE["policies"] = {}
    _INTENT_POLICY_CACHE["expires_at"] = 0.0


async def _load_intent_policies() -> Dict[str, Dict[str, Any]]:
    now = _time_mod.monotonic()
    cached_policies = _INTENT_POLICY_CACHE.get("policies")
    cached_expires_at = float(_INTENT_POLICY_CACHE.get("expires_at") or 0.0)
    if isinstance(cached_policies, dict) and cached_expires_at > now:
        return cached_policies

    try:
        try:
            from app.db import get_pool  # type: ignore
        except ImportError:
            from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT intent, default_model, cascade_downgrade, allowed_models FROM intent_policies"
            )
    except Exception as exc:
        logger.warning("intent_policies_load_failed: %s", exc)
        _INTENT_POLICY_CACHE["policies"] = {}
        _INTENT_POLICY_CACHE["expires_at"] = now + _INTENT_POLICY_CACHE_TTL_SECONDS
        return {}

    policies: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        intent = str(record.get("intent") or "").strip()
        if not intent:
            continue

        allowed_models: List[str] = []
        for allowed_model in record.get("allowed_models") or []:
            normalized_model = _normalize_intent_policy_model(allowed_model)
            if normalized_model and normalized_model not in allowed_models:
                allowed_models.append(normalized_model)

        policies[intent] = {
            "default_model": _normalize_intent_policy_model(record.get("default_model")),
            "cascade_downgrade": bool(record.get("cascade_downgrade")),
            "allowed_models": allowed_models,
        }

    _INTENT_POLICY_CACHE["policies"] = policies
    _INTENT_POLICY_CACHE["expires_at"] = now + _INTENT_POLICY_CACHE_TTL_SECONDS
    return policies


def _resolve_intent_policy_cascade_model(current_model: str, policy: Optional[Dict[str, Any]]) -> Optional[str]:
    if not policy:
        return None

    current_policy_model = _normalize_intent_policy_model(current_model)
    current_rank = _INTENT_POLICY_CLAUDE_RANK.get(current_policy_model)
    if current_rank is None:
        return None

    allowed_models = [
        _normalize_intent_policy_model(model)
        for model in (policy.get("allowed_models") or [])
    ]
    allowed_claude_ranks = sorted(
        {
            _INTENT_POLICY_CLAUDE_RANK[model]
            for model in allowed_models
            if model in _INTENT_POLICY_CLAUDE_RANK
        }
    )
    if not allowed_claude_ranks:
        default_model = _normalize_intent_policy_model(policy.get("default_model"))
        default_rank = _INTENT_POLICY_CLAUDE_RANK.get(default_model)
        if default_rank is not None:
            allowed_claude_ranks = [default_rank]

    if not allowed_claude_ranks:
        return None

    target_rank: Optional[int] = None
    if bool(policy.get("cascade_downgrade")):
        candidate_ranks = [rank for rank in allowed_claude_ranks if rank <= current_rank]
        if candidate_ranks:
            target_rank = min(candidate_ranks)
    elif current_rank not in allowed_claude_ranks:
        candidate_ranks = [rank for rank in allowed_claude_ranks if rank <= current_rank]
        if candidate_ranks:
            target_rank = max(candidate_ranks)

    if target_rank is None or target_rank >= current_rank:
        return None
    if target_rank == _INTENT_POLICY_CLAUDE_RANK["claude-haiku"]:
        return "claude-haiku"
    if target_rank == _INTENT_POLICY_CLAUDE_RANK["claude-sonnet"]:
        return "claude-sonnet"
    return None


def _resolve_legacy_intent_cascade_model(current_model: str, intent: str) -> Optional[str]:
    if intent in _HAIKU_FALLBACK_INTENTS and current_model in ("claude-sonnet", "claude-opus"):
        return "claude-haiku"
    if intent in _SONNET_INTENTS and current_model == "claude-opus":
        return "claude-sonnet"
    return None


def _build_intent_resolution_result(
    *,
    intent: str,
    input_model: str,
    resolved_model: Optional[str],
    applied: bool,
    reason: str,
    source: str,
) -> Dict[str, Any]:
    return {
        "intent": intent,
        "input_model": input_model,
        "selected_model": resolved_model or input_model,
        "applied": bool(applied),
        "reason": reason,
        "source": source,
    }


def _summarize_intent_resolution_diff(
    legacy_result: Dict[str, Any],
    db_result: Dict[str, Any],
) -> Optional[str]:
    differences: List[str] = []
    if legacy_result.get("selected_model") != db_result.get("selected_model"):
        differences.append(
            "selected_model: "
            f"{legacy_result.get('selected_model')} -> {db_result.get('selected_model')}"
        )
    if bool(legacy_result.get("applied")) != bool(db_result.get("applied")):
        differences.append(
            "applied: "
            f"{legacy_result.get('applied')} -> {db_result.get('applied')}"
        )
    if not differences:
        return None
    return "; ".join(differences)


async def _append_governance_audit_log(
    *,
    event: str,
    mode: str,
    legacy_result: Dict[str, Any],
    db_result: Dict[str, Any],
    diff_summary: str,
    trace_id: Optional[str] = None,
) -> None:
    try:
        try:
            from app.db import get_pool  # type: ignore
        except ImportError:
            from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO governance_audit_log (
                    event, mode, legacy_result, db_result, diff_summary, trace_id
                )
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
                """,
                event,
                mode,
                json.dumps(legacy_result),
                json.dumps(db_result),
                diff_summary,
                (trace_id or "")[:64] or None,
            )
    except asyncpg.UndefinedTableError:
        logger.debug("governance_audit_log_table_missing")
    except Exception as exc:
        logger.warning("governance_audit_log_write_failed: %s", exc)


async def _resolve_governed_intent_model(
    *,
    intent: str,
    current_model: str,
    session_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    from app.core.feature_flags import get_flag

    db_primary = await get_flag("intent_policies_db_primary", default=False)
    intent_policies = await _load_intent_policies()
    intent_policy = intent_policies.get(intent)

    legacy_model = _resolve_legacy_intent_cascade_model(current_model, intent)
    db_model = _resolve_intent_policy_cascade_model(current_model, intent_policy)

    legacy_reason = "legacy no change"
    if legacy_model == "claude-haiku":
        legacy_reason = "fallback simple intent"
    elif legacy_model == "claude-sonnet":
        legacy_reason = "fallback medium intent"

    db_reason = "db policy missing"
    if db_model:
        db_reason = (
            "db policy cascade"
            if bool((intent_policy or {}).get("cascade_downgrade"))
            else "db policy allowed_models"
        )
    elif intent_policy:
        db_reason = "db policy no change"

    legacy_result = _build_intent_resolution_result(
        intent=intent,
        input_model=current_model,
        resolved_model=legacy_model,
        applied=bool(legacy_model),
        reason=legacy_reason,
        source="legacy",
    )
    db_result = _build_intent_resolution_result(
        intent=intent,
        input_model=current_model,
        resolved_model=db_model,
        applied=bool(db_model),
        reason=db_reason,
        source="db",
    )

    if db_primary:
        if db_model:
            return db_model, db_reason
        return legacy_model, legacy_reason if legacy_model else None

    diff_summary = _summarize_intent_resolution_diff(legacy_result, db_result)
    if diff_summary:
        await _append_governance_audit_log(
            event="intent_resolve",
            mode="shadow",
            legacy_result=legacy_result,
            db_result=db_result,
            diff_summary=diff_summary,
            trace_id=session_id,
        )

    if legacy_model:
        return legacy_model, legacy_reason
    return None, None


async def _relay_clear_aads_session_for_oauth_fallback(session_id: Optional[str]) -> None:
    """OAuth 슬롯 전환(Gmail→Naver) 전에 호출.

    Relay는 session_id당 CLI session_id를 하나만 저장한다. 슬롯1에서 만든 CLI 세션으로
    슬롯2 토큰이 --resume 하면 인증/계정 불일치로 실패한다. 폴백 전 매핑 제거 필수.
    """
    if not session_id:
        return
    _cli_session_map.pop(session_id, None)
    if not _CLAUDE_CLI_ENABLED:
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = await client.delete("{}/sessions/{}".format(_CLAUDE_RELAY_URL, session_id))
            if resp.status_code not in (200, 404):
                logger.warning("relay_session_clear: HTTP %s", resp.status_code)
    except Exception as ex:
        logger.warning("relay_session_clear_failed: %s", ex)


# AADS-186E-2: Extended Thinking 전역 스위치 (기본 활성화)
_EXTENDED_THINKING_ENABLED = os.getenv("EXTENDED_THINKING_ENABLED", "true").lower() == "true"

# 모델별 비용 (per 1M tokens, USD)
_COST_MAP = {
    "claude-opus":            (5.0,  25.0),   # Opus 4.7 실제 가격
    "claude-opus-46":         (5.0,  25.0),   # Opus 4.6 실제 가격
    "claude-sonnet":          (3.0,  15.0),
    "claude-haiku":           (1.0,   5.0),   # Haiku 4.5 실제 가격
    "gemini-flash":           (0.075, 0.3),
    "gemini-flash-lite":      (0.01,  0.04),
    "gemini-pro":             (1.25,  5.0),
    "gemini-3-flash-preview": (0.5,   3.0),   # 2026-03-12 공식 가격
    "gemini-3.1-flash-lite-preview": (0.25, 1.5),  # 최고 효율 (thinking 포함)
    "gemini-3.1-pro-preview": (2.0,  12.0),
    "gemini-2.5-flash":       (0.15,  0.6),   # thinking 별도 $3.50 (여기선 non-thinking만)
    "gemini-2.5-flash-lite":  (0.04,  0.1),
    # Groq (무료 — 비용 0)
    "groq-qwen3-32b":        (0.0,   0.0),
    "groq-kimi-k2":          (0.0,   0.0),

    "groq-llama4-scout":     (0.0,   0.0),
    # OpenAI (LiteLLM 경유)
    "gpt-4o":                (2.50,  10.0),
    "gpt-4o-mini":           (0.15,   0.6),
    "gpt-5":                 (5.0,   15.0),
    "gpt-5-mini":            (0.50,   2.0),
    "o3":                    (2.0,    8.0),
    "o3-mini":               (1.10,   4.40),
    "o3-pro":                (20.0,  80.0),
    # Codex CLI (ChatGPT Plus OAuth)
    "gpt-5.4":               (2.50,  15.0),
    "gpt-5.4-mini":          (0.75,   4.50),
    "gpt-5.3-codex":         (1.75,  14.0),
    "groq-llama-70b":        (0.0,   0.0),
    "groq-llama-8b":         (0.0,   0.0),
    "groq-gpt-oss-120b":     (0.0,   0.0),
    "groq-compound":         (0.0,   0.0),
    # DeepSeek
    "deepseek-chat":         (0.28,  0.42),
    "deepseek-reasoner":     (0.55,  2.19),
    # OpenRouter
    "openrouter-grok-4-fast":    (0.20,  0.20),   # Grok 4.1 Fast, 2M ctx
    "openrouter-deepseek-v3":    (0.26,  0.26),   # DeepSeek V3.2
    "openrouter-mistral-small":  (0.15,  0.15),   # Mistral Small
    "openrouter-nemotron-free":  (0.0,   0.0),    # Nemotron 3 Super (무료)
    "openrouter-minimax-m2":     (0.30,  0.30),   # MiniMax M2.7
    # Alibaba/Qwen (DashScope via LiteLLM)
    "qwen3-235b":              (0.60,  2.40),
    "qwen3-235b-instruct":     (0.60,  2.40),
    "qwen3-235b-thinking":     (0.60,  2.40),
    "qwen3-next-80b":          (0.30,  1.20),
    "qwen3-max":               (0.40,  1.20),
    "qwen3-32b":               (0.08,  0.32),
    "qwen3-30b-a3b":           (0.07,  0.28),
    "qwen3-14b":               (0.04,  0.16),
    "qwen3-8b":                (0.02,  0.08),
    "qwen3-coder-plus":        (0.35,  1.40),
    "qwen3-coder-flash":       (0.07,  0.28),
    "qwen3-coder-480b":        (1.20,  4.80),
    "qwen3.5-plus":            (0.40,  1.20),
    "qwen3.5-flash":           (0.07,  0.28),
    "qwen-max":                (0.40,  1.20),
    "qwen-max-latest":         (0.40,  1.20),
    "qwen-plus":               (0.08,  0.32),
    "qwen-plus-latest":        (0.08,  0.32),
    "qwen-turbo":              (0.02,  0.06),
    "qwen-turbo-latest":       (0.02,  0.06),
    "qwen-flash":              (0.01,  0.03),
    "qwen-coder-plus":         (0.35,  1.40),
    "qwen2.5-72b-instruct":    (0.30,  0.90),
    "qwq-plus":                (0.60,  2.40),
    # Alibaba/Qwen 멀티모달 (Vision/Omni)
    "qwen-vl-max":             (0.40,  1.20),
    "qwen-vl-plus":            (0.08,  0.32),
    "qwen3-vl-plus":           (0.35,  1.40),
    "qwen3-vl-235b":           (0.60,  2.40),
    "qwen-omni-turbo":         (0.02,  0.06),
    # DashScope DeepSeek (Alibaba 호스팅)
    "dashscope-deepseek-v3.2": (0.28,  0.42),
    # Kimi (Moonshot AI, LiteLLM 경유)
    "kimi-k2.5":               (0.60,  2.40),
    "kimi-k2":                 (0.60,  2.40),
    "kimi-latest":             (0.02,  0.06),
    "kimi-128k":               (0.06,  0.24),
    "kimi-8k":                 (0.02,  0.06),
    # MiniMax (TokenPlan, LiteLLM 경유)
    "minimax-m2.7":            (0.50,  2.00),
    "minimax-m2.5":            (0.30,  1.20),
}

# LiteLLM alias → Anthropic model ID
_ANTHROPIC_MODEL_ID = {
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-opus":   "claude-opus-4-7",
    "claude-opus-46": "claude-opus-4-6",
    "claude-haiku":  "claude-haiku-4-5-20251001",
}

# Gemini 모델 (LiteLLM 경유) — 대시보드 ModelSelector id와 동기화 필수 (누락 시 Claude로 폴백됨)
_GEMINI_MODELS = {
    "gemini-flash",
    "gemini-flash-lite",
    "gemini-pro",
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-flash-image",
    "gemma-3-27b-it",
}

# Gemini Thinking 모델 — reasoning_effort=low + 높은 max_tokens 필요
_GEMINI_THINKING_MODELS = {
    "gemini-pro",
    "gemini-flash",
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
}

# Groq 모델 (LiteLLM 경유, 무료)
_GROQ_MODELS = {"groq-qwen3-32b", "groq-kimi-k2", "groq-llama4-scout", "groq-llama-70b", "groq-llama-8b", "groq-gpt-oss-120b", "groq-compound"}
# OpenAI 모델 (LiteLLM 경유)
_OPENAI_MODELS = {"gpt-4o", "gpt-4o-mini", "gpt-5", "gpt-5-mini", "o3", "o3-mini", "o3-pro"}

# Codex CLI 모델 (ChatGPT Plus OAuth, relay /codex-stream 경유)
_CODEX_MODELS = {"gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"}
_CODEX_MODEL_DISPLAY = {"gpt-5.4": "GPT-5.4 (Codex CLI)", "gpt-5.4-mini": "GPT-5.4 Mini (Codex CLI)", "gpt-5.3-codex": "GPT-5.3 Codex (Codex CLI)"}

# DeepSeek 모델 (LiteLLM 경유)
_DEEPSEEK_MODELS = {"deepseek-chat", "deepseek-reasoner"}

# OpenRouter 모델 (LiteLLM 경유, openrouter/ prefix)
_OPENROUTER_MODELS = {
    "openrouter-grok-4-fast",
    "openrouter-deepseek-v3",
    "openrouter-mistral-small",
    "openrouter-nemotron-free",
    "openrouter-minimax-m2",
}

# Kimi 모델 (Moonshot AI, LiteLLM 경유)
_KIMI_MODELS = {"kimi-k2.5", "kimi-k2", "kimi-latest", "kimi-128k", "kimi-8k"}

# MiniMax 모델 (LiteLLM 경유)
_MINIMAX_MODELS = {"minimax-m2.7", "minimax-m2.5"}

# Alibaba/Qwen 모델 (LiteLLM 경유, DashScope)
_ALIBABA_MODELS = {
    # Qwen3 플래그십
    "qwen3-235b",
    "qwen3-235b-instruct",
    "qwen3-235b-thinking",
    "qwen3-next-80b",
    "qwen3-max",
    "qwen3-32b",
    "qwen3-30b-a3b",
    "qwen3-14b",
    "qwen3-8b",
    # Qwen3 코더
    "qwen3-coder-plus",
    "qwen3-coder-flash",
    "qwen3-coder-480b",
    # Qwen3.5
    "qwen3.5-plus",
    "qwen3.5-flash",
    # Qwen (안정 릴리스)
    "qwen-max",
    "qwen-max-latest",
    "qwen-plus",
    "qwen-plus-latest",
    "qwen-turbo",
    "qwen-turbo-latest",
    "qwen-flash",
    "qwen-coder-plus",
    # Qwen2.5
    "qwen2.5-72b-instruct",
    # 추론
    "qwq-plus",
    # 멀티모달 (Vision/Omni)
    "qwen-vl-max",
    "qwen-vl-plus",
    "qwen3-vl-plus",
    "qwen3-vl-235b",
    "qwen-omni-turbo",
    # DashScope DeepSeek (Alibaba 호스팅)
    "dashscope-deepseek-v3.2",
}

# LiteLLM OpenAI 호환 모델 (Gemini + Groq + DeepSeek + OpenRouter + Alibaba)
_LITELLM_OPENAI_MODELS = _GEMINI_MODELS | _GROQ_MODELS | _DEEPSEEK_MODELS | _OPENROUTER_MODELS | _ALIBABA_MODELS | _KIMI_MODELS | _MINIMAX_MODELS | _OPENAI_MODELS | _CODEX_MODELS

_OPENAI_COMPATIBLE_DIRECT_PROVIDERS = {"openai", "groq", "deepseek", "openrouter", "qwen", "kimi", "minimax"}
_DIRECT_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.ai/v1",
    "minimax": "https://api.minimax.chat/v1",
}
_DIRECT_PROVIDER_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "qwen": "ALIBABA_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}


async def get_available_model_ids() -> set[str]:
    executable = await _get_registry_executable_model_ids()
    return executable or (_LITELLM_OPENAI_MODELS | set(_ANTHROPIC_MODEL_ID.keys()))


async def get_display_models(active_only: bool = True) -> list[dict[str, Any]]:
    return await _list_registered_models(active_only=active_only)


async def _get_registered_model_row(model_id: str) -> Optional[Dict[str, Any]]:
    rows = await _list_registered_models(active_only=False)
    for row in rows:
        if row.get("model_id") == model_id:
            return row
    return None


def _coerce_metadata(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("registered_model_metadata_invalid_json")
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    if value is None:
        return {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        logger.warning("registered_model_metadata_invalid_type: %s", type(value).__name__)
        return {}


def _route_metadata(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    metadata = _coerce_metadata((row or {}).get("metadata"))
    backend = str(metadata.get("execution_backend") or "").strip()
    if backend == "openai_compatible_direct":
        return metadata
    return {}


async def _get_direct_provider_api_key(provider: str) -> str:
    keys = await _get_provider_keys(provider)
    if keys:
        return keys[0]
    env_name = _DIRECT_PROVIDER_ENV_KEYS.get(provider, "")
    return os.getenv(env_name, "") if env_name else ""


async def _stream_direct_openai_provider(
    display_model: str,
    provider: str,
    metadata: Dict[str, Any],
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    request_model = str(metadata.get("execution_model_id") or display_model).strip() or display_model
    base_url = str(metadata.get("execution_base_url") or _DIRECT_PROVIDER_BASE_URLS.get(provider, "")).rstrip("/")
    api_key = await _get_direct_provider_api_key(provider)
    if not base_url or not api_key:
        yield {"type": "error", "content": f"direct provider route unavailable: provider={provider}"}
        return
    async for event in _stream_litellm_openai(
        request_model,
        system_prompt,
        messages,
        tools,
        session_id=session_id,
        base_url=base_url,
        api_key=api_key,
        display_model=display_model,
        cost_model=display_model,
    ):
        yield event


def _fallback_for_unavailable_model(model: str, available_models: set[str]) -> str:
    groups = [
        [candidate for candidate in ("claude-sonnet", "claude-haiku", "claude-opus", "claude-opus-46") if candidate in available_models],
        [candidate for candidate in ("gemini-2.5-flash", "gemini-flash", "gemini-3-flash-preview", "gemini-2.5-pro") if candidate in available_models],
        [candidate for candidate in ("gpt-4o-mini", "gpt-4o", "gpt-5-mini", "gpt-5") if candidate in available_models],
        [candidate for candidate in ("qwen-turbo", "qwen-flash", "qwen-plus", "qwen-max") if candidate in available_models],
        [candidate for candidate in ("deepseek-chat", "groq-compound", "minimax-m2.7", "kimi-latest") if candidate in available_models],
    ]
    for candidates in groups:
        if candidates:
            return candidates[0]
    return next(iter(sorted(available_models))) if available_models else model


def _estimate_cost(model: str, in_tokens: int, out_tokens: int) -> Decimal:
    in_rate, out_rate = _COST_MAP.get(model, (3.0, 15.0))
    return Decimal(str(round(in_tokens * in_rate / 1_000_000 + out_tokens * out_rate / 1_000_000, 6)))


async def call_stream(
    intent_result: IntentResult,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model_override: Optional[str] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    인텐트 결과에 따라 LiteLLM 또는 Anthropic SDK로 SSE 스트리밍.

    Yields dict with keys:
      type: 'delta' | 'thinking' | 'tool_use' | 'tool_result' | 'done' | 'error'
      content: str (delta/error)
      thinking: str (thinking delta)
      tool_name: str
      tool_input: dict
      tool_use_id: str
      model: str
      cost: str
      input_tokens: int
      output_tokens: int
    """
    global _anthropic, _LITELLM_API_KEY, LITELLM_API_KEY

    _db_litellm_key = await _get_db_key("LITELLM_MASTER_KEY", "LITELLM_MASTER_KEY")
    if _db_litellm_key and _db_litellm_key != _LITELLM_API_KEY:
        _LITELLM_API_KEY = _db_litellm_key
        LITELLM_API_KEY = _db_litellm_key
        _anthropic = _get_anthropic_client()

    # mixture/auto 는 chat_service 와 동일하게 "사용자 지정 모델 없음"으로 취급해야 함.
    # 그렇지 않으면 model_override or intent 가 "mixture" 문자열이 되어 unknown → claude-sonnet 고정 등 오동작.
    _effective_override = (
        model_override
        if model_override and str(model_override).strip() not in ("mixture", "auto", "")
        else None
    )
    model = _effective_override or intent_result.model

    # FIX-4: 빈 모델명 가드 — model이 None/빈문자열이면 기본값 적용
    if not model or not str(model).strip():
        logger.warning("empty_model_fallback: model is empty/None → 'claude-sonnet'")
        model = "claude-sonnet"

    # model_override가 구체적 모델명(claude-sonnet-4-6 등)이면 LiteLLM alias로 변환
    _OVERRIDE_TO_ALIAS = {
        "claude-sonnet-4-6": "claude-sonnet", "claude-sonnet-4-5": "claude-sonnet",
        "claude-opus-4-7": "claude-opus",
        "claude-opus-4-6": "claude-opus-46", "claude-opus-4-5": "claude-opus",
        "claude-haiku-4-5": "claude-haiku",
        "claude-haiku-4-5-20251001": "claude-haiku",
        "claude-3-5-sonnet-20241022": "claude-sonnet",
        "claude-3-5-haiku-20241022":  "claude-haiku",
        "claude-3-opus-20240229":     "claude-opus",
        "claude-3-sonnet-20240229":   "claude-sonnet",
        "claude-3-haiku-20240307":    "claude-haiku",
        "claude-2.1":                 "claude-sonnet",
        "auto": "claude-sonnet",    # 레거시: 실제 auto 는 _effective_override None 으로 처리됨
    }
    if model in _OVERRIDE_TO_ALIAS:
        model = _OVERRIDE_TO_ALIAS[model]

    # ── Dynamic Model Cascading (shadow/primary governance routing) ─────────
    _intent = getattr(intent_result, "intent", "")
    _model_locked = getattr(intent_result, "model_locked", False)
    if not _effective_override and not _model_locked:
        _policy_model, _policy_reason = await _resolve_governed_intent_model(
            intent=_intent,
            current_model=model,
            session_id=session_id,
        )
        if _policy_model:
            logger.info(f"cascade_downgrade: {_intent} → {_policy_model} ({_policy_reason})")
            model = _policy_model
    else:
        if _model_locked:
            logger.info(f"cascade_skip: user explicitly selected '{model}', intent='{_intent}' — respecting user choice")

    from app.services.intent_router import resolve_intent_temperature as _rit
    _ctx_temperature.set(await _rit(_intent))

    runtime_available_models = await get_available_model_ids()
    if runtime_available_models and model not in runtime_available_models:
        fallback_model = _fallback_for_unavailable_model(model, runtime_available_models)
        logger.warning("registry_model_unavailable: '%s' -> '%s'", model, fallback_model)
        model = fallback_model

    registered_row = await _get_registered_model_row(model)
    route_metadata = _route_metadata(registered_row)
    if model not in _LITELLM_OPENAI_MODELS and model not in _ANTHROPIC_MODEL_ID and not route_metadata:
        logger.warning(f"unknown_model_fallback: '{model}' → 'claude-sonnet'")
        model = "claude-sonnet"
        registered_row = await _get_registered_model_row(model)
        route_metadata = _route_metadata(registered_row)

    # 자기 모델 질문 오답 방지: 실제 라우트 id + 제조사를 시스템 프롬프트에 명시
    _maker = "Alibaba (알리바바)" if any(q in model.lower() for q in ("qwen", "deepseek-v3")) and model in _ALIBABA_MODELS else \
             "Google (구글)" if "gemini" in model.lower() else \
             "Moonshot AI (문샷)" if "kimi" in model.lower() else \
             "MiniMax (미니맥스)" if "minimax" in model.lower() else \
             "Anthropic (앤트로픽)" if "claude" in model.lower() else \
             "Meta" if "llama" in model.lower() else ""
    _maker_line = f"이 모델의 **제조사**는 {_maker} 입니다. 제조사를 정확히 안내하세요.\n" if _maker else ""
    system_prompt = (
        system_prompt
        + "\n\n<aads_model_identity>\n"
        + "이 대화 턴 응답 생성에 사용 중인 **백엔드 라우트 모델 id**는 `"
        + model
        + "` 입니다.\n"
        + _maker_line
        + "사용자가 어떤 LLM/모델인지 물으면 위 id(및 이에 대응하는 공식 제품명, 제조사)로만 답하고, "
        + "임의로 다른 모델명이나 제조사(예: 설정과 다른 Gemini/Claude/Google)로 말하지 마세요.\n"
        + "</aads_model_identity>"
    )

    if route_metadata:
        provider = str((registered_row or {}).get("provider") or "")
        async for event in _stream_direct_openai_provider(
            model,
            provider,
            route_metadata,
            system_prompt,
            messages,
            tools=tools,
            session_id=session_id,
        ):
            yield event
        return

    # Claude 모델 → DB priority 기반 계정 교차 폴백 (rate limit은 계정별)
    _slot_records = await _get_claude_slot_records()
    _ACCOUNT_SLOTS = [slot for slot, _record in sorted(
        _slot_records.items(),
        key=lambda item: (int(item[1].get("priority", 9999)), item[0]),
    )]
    if not _ACCOUNT_SLOTS:
        _ACCOUNT_SLOTS = ["2", "1"] if _CLAUDE_RELAY_NAVER_FIRST else ["1", "2"]
    _MODEL_DOWNGRADE = {
        "claude-opus": ["claude-opus"],
        "claude-sonnet": ["claude-sonnet"],
        "claude-haiku": ["claude-haiku"],
    }
    _SAMEGRADE_FALLBACK = {
        "claude-opus": ["gpt-5.4", "gemini-3.1-pro-preview"],
        "claude-sonnet": ["gpt-5.4", "gemini-3-flash-preview"],
        "claude-haiku": ["gpt-5.4-mini", "gemini-3.1-flash-lite-preview"],
    }
    _GEMINI_SAMEGRADE = {
        "gemini-2.5-pro": "claude-opus",
        "gemini-2.5-flash": "claude-sonnet",
        "gemini-2.0-flash": "claude-haiku",
    }
    if model not in _GEMINI_MODELS and model in _ANTHROPIC_MODEL_ID:
        _original_model = model
        _downgrade = _MODEL_DOWNGRADE.get(model, [model])
        _fb_seq = []  # [(model, slot), ...]
        # 쿨다운 스마트 정렬: 사용 가능 슬롯 먼저, 쿨다운 슬롯 뒤로
        _avail = [
            s for s in _ACCOUNT_SLOTS
            if _is_slot_available(s) and not _is_db_slot_rate_limited(_slot_records.get(s))
        ]
        _db_limited = [
            s for s in _ACCOUNT_SLOTS
            if _is_slot_available(s) and _is_db_slot_rate_limited(_slot_records.get(s))
        ]
        _cooled = [s for s in _ACCOUNT_SLOTS if not _is_slot_available(s)]
        _smart_slots = _avail + _db_limited + _cooled

        async def _stream_with_slots(_target_model: str) -> AsyncGenerator[Dict[str, Any], None]:
            for _si, _slot in enumerate(_smart_slots):
                if _si > 0:
                    logger.info(f"fallback[{_si}/{len(_smart_slots)}]: {_target_model} slot={_slot}")

                _err = False
                async for event in _stream_cli_relay(_target_model, system_prompt, messages, tools=tools, session_id=session_id, oauth_slot=_slot):
                    if event.get("type") == "error":
                        _err = True
                        _err_msg = event.get("content", "")
                        logger.warning(f"relay_err: {_target_model}/slot{_slot}[{_si}] — {_err_msg[:80]}")
                        if any(k in _err_msg.lower() for k in ("429", "rate", "limit", "overloaded", "quota")):
                            _mark_slot_cooldown(_slot)
                            _slot_key = _slot_records.get(_slot, {}).get("key_name")
                            if _slot_key:
                                await _mark_key_rate_limited(_slot_key, seconds=_COOLDOWN_SECS)
                        break
                    yield event
                if not _err:
                    return

                await _relay_clear_aads_session_for_oauth_fallback(session_id)

                if _slot == _ACCOUNT_SLOTS[0]:
                    _err = False
                    logger.info(f"relay_failed: SDK for {_target_model}[{_si}]")
                    async for event in _stream_agent_sdk(_target_model, system_prompt, messages, session_id=session_id):
                        if event.get("type") == "error":
                            _err = True
                            logger.warning(f"sdk_err: {_target_model}[{_si}] — {event.get('content', '')[:80]}")
                            break
                        yield event
                    if not _err:
                        return

                logger.warning(f"tier_exhausted: {_target_model}/slot{_slot}[{_si}]")

            raise RuntimeError(f"all_slots_failed: {_target_model}")

        for _dg in _downgrade:
            for _sl in _smart_slots:
                _fb_seq.append((_dg, _sl))

        for _fi, (_fm, _fs) in enumerate(_fb_seq):
            if _fi > 0:
                logger.info(f"fallback[{_fi}/{len(_fb_seq)}]: {_fm} slot={_fs}")

            # Tier1: CLI Relay (oauth_slot으로 계정 지정)
            _err = False
            async for event in _stream_cli_relay(_fm, system_prompt, messages, tools=tools, session_id=session_id, oauth_slot=_fs):
                if event.get("type") == "error":
                    _err = True
                    _err_msg = event.get("content", "")
                    logger.warning(f"relay_err: {_fm}/slot{_fs}[{_fi}] — {_err_msg[:80]}")
                    _err_lower = _err_msg.lower()
                    # 429/한도/크레딧 오류 → 기존 쿨다운 등록
                    if any(k in _err_lower for k in ("429", "rate", "limit", "overloaded", "quota")):
                        _mark_slot_cooldown(_fs)
                        _slot_key = _slot_records.get(_fs, {}).get("key_name")
                        if _slot_key:
                            await _mark_key_rate_limited(_slot_key, seconds=_COOLDOWN_SECS)
                    # CLI exit 반복 실패는 짧은 고정 쿨다운 적용
                    elif any(k in _err_lower for k in ("cli exited", "exit code", "exited with code")):
                        _mark_slot_cooldown(_fs, duration_override=60)
                    break
                yield event
            if not _err:
                return

            await _relay_clear_aads_session_for_oauth_fallback(session_id)

            # Tier2: Agent SDK (첫 계정에서만 — 컨테이너 고정 토큰)
            if _fs == _ACCOUNT_SLOTS[0]:
                _err = False
                logger.info(f"relay_failed: SDK for {_fm}[{_fi}]")
                async for event in _stream_agent_sdk(_fm, system_prompt, messages, session_id=session_id):
                    if event.get("type") == "error":
                        _err = True
                        logger.warning(f"sdk_err: {_fm}[{_fi}] — {event.get('content', '')[:80]}")
                        break
                    yield event
                if not _err:
                    return

            logger.warning(f"tier_exhausted: {_fm}/slot{_fs}[{_fi}]")

        # 모든 Claude 모델×계정 실패 → 동급 외부 모델 순차 시도
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                import hashlib as _hl
                _eh = _hl.sha256(f"claude_api_fallback:model_selector:{model}".encode()).hexdigest()[:64]
                await conn.execute(
                    "INSERT INTO error_log (error_hash, error_type, source, server, message, stack_trace, created_at) VALUES ($1, $2, $3, $4, $5, $6, NOW()) ON CONFLICT (error_hash) DO UPDATE SET occurrence_count = error_log.occurrence_count + 1, last_seen = NOW()",
                    _eh, "claude_api_fallback", "model_selector.cli_relay_path", "aads-server",
                    f"Claude {model} → samegrade fallback 전환 (계정 교차 {' → '.join(f'{m}/s{s}' for m,s in _fb_seq)} 모두 실패)",
                    "",
                )
        except Exception as _log_err:
            logger.warning(f"error_log insert failed: {_log_err}")

        _samegrade_list = _SAMEGRADE_FALLBACK.get(_original_model, ["gemini-2.5-flash"])
        _samegrade_success = False
        for _sg_model in _samegrade_list:
            try:
                yield {"type": "delta", "content": f"\n\n⚠️ _Claude 일시 장애 — {_sg_model}로 전환하여 계속합니다._\n\n"}
                _sg_had_error = False
                if _sg_model in _CODEX_MODELS:
                    _sg_stream = _stream_codex_relay(_sg_model, system_prompt, messages, tools=tools, session_id=session_id)
                else:
                    _sg_stream = _stream_litellm(_sg_model, system_prompt, messages, tools=tools)
                async for event in _sg_stream:
                    if isinstance(event, dict) and event.get("type") == "error":
                        _sg_had_error = True
                        logger.warning(f"samegrade_fallback_failed: {_original_model} -> {_sg_model}: {event.get('content', '')[:120]}")
                        break
                    yield event
                if not _sg_had_error:
                    _samegrade_success = True
                    break
            except Exception as _sg_err:
                logger.warning(f"samegrade_fallback_failed model={_sg_model}: {_sg_err}")
                continue

        if _original_model in ("claude-opus",) and not _samegrade_success:
            logger.warning(f"all_samegrade_failed: {_original_model} → last_resort claude-sonnet")
            try:
                async for event in _stream_with_slots("claude-sonnet"):
                    yield event
                return
            except Exception as e:
                logger.error(f"last_resort_sonnet_failed: {e}")

        if not _samegrade_success:
            yield {"type": "delta", "content": "\n\n⚠️ _전체 LLM 장애 — 잠시 후 다시 시도해주세요._\n\n"}
            yield {"type": "error", "content": "All LLM providers failed"}
        return

    # Gemini 모델 → LiteLLM 경유 (실패 시 동급 Claude 우선 폴백)
    if model in _GEMINI_MODELS:
        _had_error = False
        async for event in _stream_litellm(model, system_prompt, messages, tools=tools):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"gemini_fallback: {model} failed, falling back to same-grade model")
                break
            yield event
        if _had_error:
            _fallback_model = _GEMINI_SAMEGRADE.get(model, "claude-sonnet")
            _fallback_intent = IntentResult(
                intent=intent_result.intent,
                model=_fallback_model,
                use_tools=intent_result.use_tools,
                tool_group=intent_result.tool_group,
            )
            yield {"type": "delta", "content": f"\n\n⚠️ _{model} 장애 — {_fallback_model}로 전환하여 계속합니다._\n\n"}
            async for event in _stream_anthropic(_fallback_intent, _fallback_model, system_prompt, messages, tools, session_id=session_id):
                yield event
        return

    # Groq / DeepSeek 모델 → LiteLLM 경유 (OpenAI 호환, 실패 시 Gemini Flash 폴백)
    if model in _GROQ_MODELS or model in _DEEPSEEK_MODELS:
        _had_error = False
        async for event in _stream_litellm(model, system_prompt, messages, tools=tools):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"litellm_fallback: {model} failed, falling back to gemini-2.5-flash")
                break
            yield event
        if _had_error:
            yield {"type": "delta", "content": f"\n\n[{model} 오류 → Gemini Flash 전환]\n\n"}
            async for event in _stream_litellm("gemini-2.5-flash", system_prompt, messages, tools=tools):
                if event.get("type") in ("done", "model_info"):
                    event = {**event, "model": model}
                yield event
        return

    # OpenRouter 모델 → LiteLLM 경유 (openrouter/ prefix 붙여서 전달, 실패 시 Gemini Flash 폴백)
    if model in _OPENROUTER_MODELS:
        _or_model = model
        _had_error = False
        async for event in _stream_litellm_openai(_or_model, system_prompt, messages, tools=tools):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"openrouter_fallback: {model} ({_or_model}) failed, falling back to gemini-2.5-flash")
                break
            if event.get("type") in ("done", "model_info"):
                event = {**event, "model": model}
            yield event
        if _had_error:
            yield {"type": "delta", "content": f"\n\n[{model} 오류 → Gemini Flash 전환]\n\n"}
            async for event in _stream_litellm("gemini-2.5-flash", system_prompt, messages, tools=tools):
                if event.get("type") in ("done", "model_info"):
                    event = {**event, "model": model}
                yield event
        return

    # Alibaba/Qwen 모델 → LiteLLM 경유 (DashScope, 실패 시 Gemini Flash 폴백)
    if model in _ALIBABA_MODELS:
        _had_error = False
        async for event in _stream_litellm_openai(model, system_prompt, messages, tools=tools):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"alibaba_fallback: {model} failed, falling back to gemini-2.5-flash")
                break
            if event.get("type") in ("done", "model_info"):
                event = {**event, "model": model}
            yield event
        if _had_error:
            yield {"type": "delta", "content": f"\n\n[{model} 오류 → Gemini Flash 전환]\n\n"}
            async for event in _stream_litellm("gemini-2.5-flash", system_prompt, messages, tools=tools):
                if event.get("type") in ("done", "model_info"):
                    event = {**event, "model": model}
                yield event
        return


    # Kimi / MiniMax 모델 → LiteLLM 경유 (실패 시 Gemini Flash 폴백)
    if model in _KIMI_MODELS or model in _MINIMAX_MODELS:
        _had_error = False
        async for event in _stream_litellm_openai(model, system_prompt, messages, tools=tools):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"kimi_minimax_fallback: {model} failed, falling back to gemini-2.5-flash")
                break
            if event.get("type") in ("done", "model_info"):
                event = {**event, "model": model}
            yield event
        if _had_error:
            yield {"type": "delta", "content": f"\n\n[{model} 오류 → Gemini Flash 전환]\n\n"}
            async for event in _stream_litellm("gemini-2.5-flash", system_prompt, messages, tools=tools):
                if event.get("type") in ("done", "model_info"):
                    event = {**event, "model": model}
                yield event
        return

    # Codex CLI 모델 → Relay /codex-stream 경유 (ChatGPT Plus OAuth, 실패 시 Gemini Flash 폴백)
    if model in _CODEX_MODELS:
        _had_error = False
        async for event in _stream_codex_relay(model, system_prompt, messages, tools=tools, session_id=session_id):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"codex_fallback: {model} failed, falling back to gemini-2.5-flash")
                break
            yield event
        if _had_error:
            yield {"type": "delta", "content": f"\n\n[{model} (Codex) 오류 → Gemini Flash 전환]\n\n"}
            async for event in _stream_litellm("gemini-2.5-flash", system_prompt, messages, tools=tools):
                if event.get("type") in ("done", "model_info"):
                    event = {**event, "model": model}
                yield event
        return

    # OpenAI 모델 → LiteLLM 경유 (실패 시 Gemini Flash 폴백)
    if model in _OPENAI_MODELS:
        _had_error = False
        async for event in _stream_litellm_openai(model, system_prompt, messages, tools=tools):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"openai_fallback: {model} failed, falling back to gemini-2.5-flash")
                break
            if event.get("type") in ("done", "model_info"):
                event = {**event, "model": model}
            yield event
        if _had_error:
            yield {"type": "delta", "content": f"\n\n[{model} 오류 → Gemini Flash 전환]\n\n"}
            async for event in _stream_litellm("gemini-2.5-flash", system_prompt, messages, tools=tools):
                if event.get("type") in ("done", "model_info"):
                    event = {**event, "model": model}
                yield event
        return


def _convert_content_for_openai(content: Any) -> Any:
    """Anthropic content 블록 배열을 OpenAI/LiteLLM 포맷으로 변환."""
    if not isinstance(content, list):
        return content
    result = []
    for block in content:
        if not isinstance(block, dict):
            result.append({"type": "text", "text": str(block)})
        elif block.get("type") == "text":
            result.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image":
            source = block.get("source", {})
            if source.get("type") == "base64":
                data_url = f"data:{source['media_type']};base64,{source['data']}"
                result.append({"type": "image_url", "image_url": {"url": data_url}})
    return result if len(result) != 1 or result[0].get("type") != "text" else result[0]["text"]


async def _stream_litellm(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """LiteLLM 프록시를 통한 스트리밍.
    Claude 모델: /v1/messages?beta=true (Anthropic 네이티브, 도구 호환성 보장)
    Gemini 모델: /chat/completions (OpenAI 호환)
    """
    _is_claude = model in _ANTHROPIC_MODEL_ID

    if _is_claude:
        async for event in _stream_litellm_anthropic(model, system_prompt, messages, tools, session_id=session_id):
            yield event
    else:
        async for event in _stream_litellm_openai(model, system_prompt, messages, tools):
            yield event


async def _stream_litellm_anthropic(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Claude 모델 → LiteLLM /v1/messages?beta=true (멀티턴 도구 루프 지원)."""
    current_msgs = [m for m in messages if m.get("role") != "system"]
    litellm_model = _ANTHROPIC_MODEL_ID.get(model, model)
    _display_model = litellm_model

    full_text = ""
    input_tokens = 0
    output_tokens = 0
    _MAX_RETRIES = 10
    _MAX_TOOL_TURNS = 50
    _headers = {
        "x-api-key": LITELLM_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    _url = f"{LITELLM_BASE_URL}/v1/messages?beta=true"

    yield {"type": "model_info", "model": _display_model}

    # Prompt Caching: system_prompt -> cache_control 블록 변환
    try:
        _cached_system = _build_system_with_cache(system_prompt)
    except Exception:
        _cached_system = system_prompt
    try:
        from app.core.cache_config import build_cached_tools as _bct
        _cached_tools = _bct(tools) if tools else tools
    except Exception:
        _cached_tools = tools

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            for _turn in range(_MAX_TOOL_TURNS):
                req_body: Dict[str, Any] = {
                    "model": litellm_model,
                    "system": _cached_system,
                    "messages": current_msgs,
                    "max_tokens": _MAX_TOKENS_CLAUDE,
                    "stream": True,
                    "temperature": _ctx_temperature.get(0.2),
                }
                if _cached_tools:
                    req_body["tools"] = _cached_tools

                # 재시도 루프
                resp_ctx = None
                for _attempt in range(_MAX_RETRIES):
                    resp_ctx = client.stream("POST", _url, headers=_headers, json=req_body)
                    resp = await resp_ctx.__aenter__()
                    if resp.status_code == 200:
                        break
                    await resp.aread()
                    await resp_ctx.__aexit__(None, None, None)
                    resp_ctx = None
                    if _attempt < _MAX_RETRIES - 1:
                        logger.warning(f"litellm_anthropic_retry: turn={_turn} attempt={_attempt+1}/{_MAX_RETRIES} status={resp.status_code}")
                        await asyncio.sleep(0.3)
                    else:
                        yield {"type": "error", "content": f"Claude API {resp.status_code} after {_MAX_RETRIES} retries"}
                        return

                if resp_ctx is None:
                    yield {"type": "error", "content": "no response"}
                    return

                # SSE 파싱
                _tool_uses = []  # 이번 턴의 도구 호출들
                _assistant_content = []  # assistant 메시지 content 블록

                try:
                    _current_tool_id = ""
                    _current_tool_name = ""
                    _current_tool_input_json = ""
                    _current_text = ""

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except Exception:
                            continue

                        evt_type = event.get("type", "")

                        if evt_type == "content_block_start":
                            cb = event.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                if _current_text:
                                    _assistant_content.append({"type": "text", "text": _current_text})
                                    _current_text = ""
                                _current_tool_id = cb.get("id", "")
                                _current_tool_name = cb.get("name", "")
                                _current_tool_input_json = ""

                        elif evt_type == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    full_text += text
                                    _current_text += text
                                    yield {"type": "delta", "content": text}
                            elif delta.get("type") == "input_json_delta":
                                _current_tool_input_json += delta.get("partial_json", "")

                        elif evt_type == "content_block_stop":
                            if _current_tool_name:
                                _args = json.loads(_current_tool_input_json) if _current_tool_input_json else {}
                                _tool_uses.append({
                                    "id": _current_tool_id,
                                    "name": _current_tool_name,
                                    "input": _args,
                                })
                                _assistant_content.append({
                                    "type": "tool_use",
                                    "id": _current_tool_id,
                                    "name": _current_tool_name,
                                    "input": _args,
                                })
                                _current_tool_name = ""
                                _current_tool_id = ""
                                _current_tool_input_json = ""

                        elif evt_type == "message_delta":
                            usage = event.get("usage", {})
                            output_tokens += usage.get("output_tokens", 0)

                        elif evt_type == "message_start":
                            msg = event.get("message", {})
                            usage = msg.get("usage", {})
                            input_tokens += usage.get("input_tokens", 0)

                finally:
                    await resp_ctx.__aexit__(None, None, None)

                # 남은 텍스트 블록 추가
                if _current_text:
                    _assistant_content.append({"type": "text", "text": _current_text})

                # 도구 호출이 없으면 종료
                if not _tool_uses:
                    break

                # 도구 실행 + tool_results 구성
                tool_results = []
                from app.api.ceo_chat_tools import execute_tool as _exec_tool
                for tu in _tool_uses:
                    yield {"type": "tool_use", "tool_name": tu["name"], "tool_use_id": tu["id"], "tool_input": tu["input"]}
                    try:
                        result = await _exec_tool(tu["name"], tu["input"], "", session_id or "")
                        yield {"type": "tool_result", "tool_name": tu["name"], "content": str(result)[:3000]}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": str(result)[:10000],
                        })
                    except Exception as _te:
                        logger.warning(f"tool_error: {tu['name']}: {_te}")
                        yield {"type": "delta", "content": f"\n[도구 {tu['name']} 실패: {str(_te)[:80]}]\n"}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": f"Error: {str(_te)[:500]}",
                            "is_error": True,
                        })
                    # heartbeat (SSE 연결 유지)
                    yield {"type": "heartbeat"}

                # 다음 턴: assistant 메시지 + tool_results 추가
                current_msgs = current_msgs + [
                    {"role": "assistant", "content": _assistant_content},
                    {"role": "user", "content": tool_results},
                ]

                # CEO 인터럽트 체크 (BUG-1 FIX: LiteLLM 경로에서도 인터럽트 반영)
                if session_id:
                    from app.core.interrupt_queue import has_interrupt, pop_interrupts
                    if has_interrupt(session_id):
                        interrupts = pop_interrupts(session_id)
                        interrupt_text = "\n".join(i["content"] for i in interrupts)
                        # 인터럽트 첨부파일 → Vision content blocks
                        _intr_content: list | str = f"[CEO 추가 지시] 작업 도중 CEO가 새로운 지시를 보냈습니다. 현재까지의 작업 결과를 고려하고, 이 새 지시를 반영하여 다음 행동을 판단하세요. CEO 지시가 기존 작업과 충돌하면 CEO 지시를 우선합니다.\n\n{interrupt_text}"
                        _intr_images = []
                        for _intr_item in interrupts:
                            for att in _intr_item.get("attachments", []):
                                if att.get("type") == "image" and att.get("base64"):
                                    _intr_images.append({
                                        "type": "image_url",
                                        "image_url": {"url": f"data:{att.get('media_type', 'image/png')};base64,{att['base64']}"},
                                    })
                        if _intr_images:
                            _intr_content = [{"type": "text", "text": _intr_content}] + _intr_images
                        current_msgs.append({"role": "user", "content": _intr_content})
                        # 각 interrupt마다 개별 이벤트 yield (프론트 큐 동기화용)
                        for _intr_item in interrupts:
                            yield {"type": "interrupt_applied", "content": _intr_item["content"][:100]}

    except Exception as e:
        logger.error(f"model_selector litellm_anthropic error: {e}")
        yield {"type": "error", "content": str(e)}
        return

    cost = _estimate_cost(model, input_tokens, output_tokens)
    yield {
        "type": "done",
        "model": _display_model,
        "cost": str(cost),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


async def _stream_litellm_openai(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    display_model: Optional[str] = None,
    cost_model: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Gemini 등 비-Claude 모델 → LiteLLM /chat/completions (OpenAI 호환).
    멀티턴 Agentic Loop + 병렬 도구 실행 지원 (AADS-202).
    """
    route_base_url = (base_url or LITELLM_BASE_URL).rstrip("/")
    route_api_key = api_key or LITELLM_API_KEY
    route_display_model = display_model or model
    route_cost_model = cost_model or route_display_model

    clean_msgs = [m for m in messages if m.get("role") != "system"]
    clean_msgs = [
        {**m, "content": _convert_content_for_openai(m["content"])} for m in clean_msgs
    ]
    loop_msgs: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}] + clean_msgs

    full_text = ""
    input_tokens = 0
    output_tokens = 0

    # Thinking 모델: reasoning_effort=low로 사고 토큰 절감 + max_tokens 확대
    is_thinking = model in _GEMINI_THINKING_MODELS
    # 모델별 max_tokens 제한 (제공사 한도 초과 방지)
    _MODEL_MAX_TOKENS = {
        "deepseek-chat": 8192, "deepseek-reasoner": 8192,
        "groq-kimi-k2": 32768, "groq-llama-70b": 8192, "groq-llama-8b": 8192,
        "groq-llama4-scout": 16384, "groq-qwen3-32b": 32768,
        "groq-gpt-oss-120b": 16384, "groq-compound": 32768,
        "kimi-k2": 8192, "kimi-k2.5": 8192, "kimi-latest": 8192,
        "kimi-128k": 8192, "kimi-8k": 8192,
        "minimax-m2.7": 16384, "minimax-m2.5": 16384,
    }
    _default_max = _MAX_TOKENS_GEMINI_THINKING if is_thinking else _MAX_TOKENS_GEMINI
    max_tokens = _MODEL_MAX_TOKENS.get(model, _default_max)
    extra_params: Dict[str, Any] = {}
    if is_thinking:
        extra_params["reasoning_effort"] = "low"
    # Qwen3 계열: thinking 모드 비활성화 → 도구 호출 우선 (thinking 활성 시 도구 무시됨)
    if "qwen3" in model.lower() and "thinking" not in model.lower():
        extra_params["extra_body"] = {"enable_thinking": False}

    # OAI tools 변환 (한 번만)
    _oai_tools: List[Dict[str, Any]] = []
    if tools:
        for t in tools:
            if t.get("input_schema"):
                _oai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t["input_schema"],
                    }
                })

    MAX_TOOL_LOOPS = 500  # Claude 동일 수준 (CEO 지시)
    _consecutive_fail = 0  # 연속 도구 실패 카운터 (AADS-225-D)

    for _loop_iter in range(MAX_TOOL_LOOPS + 1):
        # 이번 턴의 tool_calls 누적 (index → {id, name, args_buf})
        _pending: Dict[int, Dict[str, str]] = {}
        _assistant_text = ""
        _finish_reason = ""

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                req_body: Dict[str, Any] = {
                    "model": model,
                    "messages": loop_msgs,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "temperature": _ctx_temperature.get(0.2),
                    **extra_params,
                }
                if _oai_tools:
                    req_body["tools"] = _oai_tools

                async with client.stream(
                    "POST",
                    f"{route_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {route_api_key}"},
                    json=req_body,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.warning(f"litellm_http_{resp.status_code}: model={route_display_model} body={body.decode()[:120]}")
                        yield {"type": "error", "content": f"LiteLLM {route_display_model} HTTP {resp.status_code}: {body.decode()[:200]}"}
                        return

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except Exception:
                            continue

                        choice = chunk.get("choices", [{}])[0]
                        delta = choice.get("delta", {})
                        fr = choice.get("finish_reason")
                        if fr:
                            _finish_reason = fr

                        # 텍스트 누적 + 스트리밍 (kimi reasoning_content fallback)
                        text = delta.get("content") or delta.get("reasoning_content") or ""
                        if text:
                            _assistant_text += text
                            full_text += text
                            yield {"type": "delta", "content": text}

                        # tool_calls 누적 (인덱스 기반, 청크 분할 대응)
                        for _tc in delta.get("tool_calls", []):
                            idx = _tc.get("index", 0)
                            if idx not in _pending:
                                _pending[idx] = {"id": "", "name": "", "args_buf": ""}
                            if _tc.get("id"):
                                _pending[idx]["id"] = _tc["id"]
                            _fn = _tc.get("function", {})
                            if _fn.get("name"):
                                _pending[idx]["name"] = _fn["name"]
                            if _fn.get("arguments"):
                                _pending[idx]["args_buf"] += _fn["arguments"]

                        # 토큰 집계
                        usage = chunk.get("usage", {})
                        if usage:
                            input_tokens = usage.get("prompt_tokens", input_tokens)
                            output_tokens = usage.get("completion_tokens", output_tokens)

        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
            logger.warning(f"litellm_network_error: model={route_display_model} error={e}")
            yield {"type": "error", "content": f"LiteLLM {route_display_model} network error: {str(e)[:200]}"}
            return
        except Exception as e:
            logger.error(f"model_selector litellm error: {e}")
            yield {"type": "error", "content": str(e)}
            return

        # ── Gemini 빈응답 감지: 텍스트도 tool_calls도 없으면 에러 yield (AADS-236) ──
        if not full_text.strip() and not _pending:
            logger.warning(f"litellm_empty_response: model={model} — no text delta received")
            yield {"type": "error", "content": "empty_response"}
            return

        # 도구 호출 없거나 finish_reason이 stop → 루프 종료
        if _finish_reason != "tool_calls" or not _pending:
            break

        # ── Agentic Loop: 도구 실행 후 Gemini 재호출 ──
        if _loop_iter >= MAX_TOOL_LOOPS:
            logger.warning(f"gemini_tool_loop_max_reached: model={model} loops={_loop_iter}")
            break

        # assistant 메시지 (tool_calls 포함) 추가
        _sorted_tcs = sorted(_pending.values(), key=lambda x: x["id"])
        _assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["args_buf"]},
                }
                for tc in _sorted_tcs
            ],
        }
        if _assistant_text:
            _assistant_msg["content"] = _assistant_text
        loop_msgs.append(_assistant_msg)

        # 병렬 도구 실행
        from app.api.ceo_chat_tools import execute_tool as _exec_tool  # noqa: PLC0415

        async def _run_one(tc: Dict[str, str]) -> Dict[str, Any]:
            try:
                _args = json.loads(tc["args_buf"]) if isinstance(tc["args_buf"], str) else tc["args_buf"]
            except Exception:
                _args = {}
            try:
                _res = await _exec_tool(tc["name"], _args, "", session_id or "")
                return {"id": tc["id"], "name": tc["name"], "args": _args, "result": str(_res)[:4000], "ok": True}
            except Exception as _te:
                logger.warning(
                    "gemini_tool_error: session=%s tool=%s error_type=%s error=%s args=%s",
                    (session_id or "")[:8], tc["name"], type(_te).__name__,
                    str(_te)[:200], str(_args)[:100],
                )
                return {"id": tc["id"], "name": tc["name"], "args": _args, "result": f"도구 실행 오류: {str(_te)[:200]}", "ok": False}

        # 도구 실행 전 tool_use 이벤트를 먼저 전송해 프론트가 즉시 표시할 수 있게 한다.
        for tc in _sorted_tcs:
            try:
                _args = json.loads(tc["args_buf"]) if isinstance(tc["args_buf"], str) else tc["args_buf"]
            except Exception:
                _args = {}
            yield {"type": "tool_use", "tool_name": tc["name"], "tool_use_id": tc["id"], "tool_input": _args}

        _exec_results = await asyncio.gather(*[_run_one(tc) for tc in _sorted_tcs])

        # 실행 결과만 yield + tool 메시지 추가
        for _er in _exec_results:
            yield {"type": "tool_result", "tool_name": _er["name"], "content": _er["result"]}
            loop_msgs.append({
                "role": "tool",
                "tool_call_id": _er["id"],
                "content": _er["result"],
            })

        # 연속 실패 감지 (AADS-225-D): 배치 내 전부 실패 → 카운터++, 하나라도 성공 → 리셋
        if all(not _er["ok"] for _er in _exec_results):
            _consecutive_fail += 1
            logger.warning(
                "gemini_tool_all_failed: session=%s iter=%d consecutive=%d tools=%s",
                (session_id or "")[:8], _loop_iter + 1, _consecutive_fail,
                [e["name"] for e in _exec_results],
            )
            if _consecutive_fail >= 3:
                logger.error(
                    "gemini_tool_loop_break: session=%s model=%s consecutive=3",
                    (session_id or "")[:8], model,
                )
                yield {"type": "delta", "content": "\n\n[도구 3회 연속 실패 — 루프를 중단합니다]\n"}
                break
        else:
            _consecutive_fail = 0

        logger.info(f"gemini_tool_loop: iter={_loop_iter+1} tools={[e['name'] for e in _exec_results]}")
        # loop_iter 증가 후 재호출

    # 루프 종료 후 빈 응답 체크 — Gemini가 텍스트 없이 [DONE]만 전송한 경우
    if not full_text.strip() and not _pending:
        logger.warning(f"litellm_empty_response: model={model} — no text delta received")
        yield {"type": "error", "content": "empty_response"}
        return

    cost = _estimate_cost(route_cost_model, input_tokens, output_tokens)
    yield {
        "type": "done",
        "model": route_display_model,
        "cost": str(cost),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


async def _stream_claude_sonnet_fallback(
    messages: List[Dict[str, Any]],
    system_prompt: str,
    tools: Optional[List[Dict[str, Any]]],
    session_id: Optional[str],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Gemini/LiteLLM 실패 시 Claude Sonnet으로 폴백 스트리밍 (Tier1: CLI Relay)."""
    async for event in _stream_cli_relay(
        "claude-sonnet", system_prompt, messages, tools=tools, session_id=session_id
    ):
        yield event


async def _stream_cli_relay(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
    oauth_slot: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """CLI Relay 서버(host.docker.internal:8199)를 통한 스트리밍.

    호스트 최신 CLI를 사용하므로 OAuth 인증 안정성이 높음.
    NDJSON 응답을 파싱하여 AADS SSE 이벤트로 변환.
    """
    sdk_model = _ANTHROPIC_MODEL_ID.get(model, model)

    # 세션 이어가기 여부
    _has_resume = bool(_cli_session_map.get(session_id)) if session_id else False
    formatted = _format_messages_for_llm(messages, has_resume=_has_resume)

    req_body: Dict[str, Any] = {
        "model": model,
        "system_prompt": system_prompt,
        "session_id": session_id or "",
        "temperature": _ctx_temperature.get(0.2),
    }
    if oauth_slot:
        req_body["oauth_slot"] = oauth_slot

    if isinstance(formatted, list):
        # 이미지 포함: content block 배열로 전달 → relay가 --input-format stream-json 사용
        req_body["content_blocks"] = formatted
        req_body["messages_text"] = ""  # 하위 호환
    else:
        req_body["messages_text"] = formatted

    yield {"type": "model_info", "model": sdk_model}

    # CLI가 에러를 텍스트로 반환하는 패턴 감지 (529, 401 등)
    _CLI_ERROR_PATTERNS = [
        "api error:", "overloaded", "529", "503",
        "authentication_failed", "401", "unauthorized",
        "rate_limit", "429", "rate limit",
        "credit", "402",
    ]

    full_text = ""
    _tool_id_to_name: Dict[str, str] = {}
    _captured_cli_sid = _cli_session_map.get(session_id, "") if session_id else ""

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            # Health check first (빠른 실패)
            try:
                hc = await client.get(f"{_CLAUDE_RELAY_URL}/health", timeout=5.0)
                if hc.status_code != 200:
                    yield {"type": "error", "content": f"CLI Relay not healthy: {hc.status_code}"}
                    return
            except Exception as hc_err:
                yield {"type": "error", "content": f"CLI Relay unreachable: {hc_err}"}
                return

            async with client.stream(
                "POST",
                f"{_CLAUDE_RELAY_URL}/stream",
                json=req_body,
                timeout=httpx.Timeout(600.0, connect=10.0),
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield {"type": "error", "content": f"CLI Relay {resp.status_code}: {body.decode()[:200]}"}
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # result 이벤트에서 is_error 체크 (CLI가 529 등으로 실패 시)
                    if event.get("type") == "result" and event.get("is_error"):
                        error_text = event.get("result", "CLI error")
                        logger.warning(f"cli_relay_result_error: {error_text[:100]}")
                        yield {"type": "error", "content": error_text}
                        return

                    # _map_cli_event 재사용하여 NDJSON → AADS 이벤트 변환
                    mapped = _map_cli_event(event, session_id=session_id)
                    if mapped is None:
                        # init 이벤트에서 session_id 캡처
                        evt_type = event.get("type", "")
                        if evt_type == "system" and event.get("subtype") == "init":
                            sid = event.get("session_id")
                            if sid:
                                _captured_cli_sid = sid
                        continue

                    for aads_evt in mapped:
                        evt_type = aads_evt.get("type", "")

                        # tool_use에서 tool_id→name 매핑 저장
                        if evt_type == "tool_use":
                            tid = aads_evt.get("tool_use_id", "")
                            tname = aads_evt.get("tool_name", "")
                            if tid and tname:
                                _tool_id_to_name[tid] = tname

                        # tool_result에 tool_name 복원
                        if evt_type == "tool_result" and not aads_evt.get("tool_name"):
                            tid = aads_evt.get("tool_use_id", "")
                            aads_evt["tool_name"] = _tool_id_to_name.get(tid, "")
                        if evt_type == "tool_result" and aads_evt.get("is_error"):
                            logger.warning(
                                "cli_relay_tool_error: session=%s tool=%s error_type=%s content=%s",
                                (session_id or "default")[:8],
                                aads_evt.get("tool_name", ""),
                                aads_evt.get("error_type", "tool_error"),
                                str(aads_evt.get("raw_error") or aads_evt.get("content", ""))[:240],
                            )

                        # delta 텍스트 누적 (에러 패턴 검사는 result.is_error로 대체)
                        if evt_type == "delta":
                            delta_text = aads_evt.get("content", "")
                            full_text += delta_text

                        # done 이벤트에서 session_id 캡처 (result 이벤트)
                        if evt_type == "done":
                            result_sid = event.get("session_id")
                            if result_sid:
                                _captured_cli_sid = result_sid

                        yield aads_evt

    except httpx.ConnectError as e:
        yield {"type": "error", "content": f"CLI Relay connect failed: {e}"}
        return
    except httpx.ReadTimeout:
        yield {"type": "error", "content": "CLI Relay timeout (600s)"}
        return
    except Exception as e:
        logger.error(f"cli_relay_error: {e}")
        yield {"type": "error", "content": str(e)}
        return

    # 세션 매핑 저장
    if session_id and _captured_cli_sid:
        _cli_session_map[session_id] = _captured_cli_sid
        logger.info(f"cli_relay_session_map: aads={session_id[:8]} -> cli={_captured_cli_sid[:8]}")


_CODEX_RETRY_DELAYS = (2.0, 5.0)
_CODEX_RETRYABLE_ERROR_MARKERS = (
    "timeout",
    "temporarily unavailable",
    "temporary failure",
    "temporarily overloaded",
    "relay unreachable",
    "not healthy: 5",
    "relay 500",
    "relay 502",
    "relay 503",
    "relay 504",
    "connect failed",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "econnreset",
    "network is unreachable",
)
_CODEX_NON_RETRYABLE_ERROR_MARKERS = (
    "401",
    "403",
    "404",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "authentication",
    "permission denied",
)


def _is_codex_retryable_error(error_content: str) -> bool:
    lowered = str(error_content or "").lower()
    if not lowered:
        return False
    if any(marker in lowered for marker in _CODEX_NON_RETRYABLE_ERROR_MARKERS):
        return False
    return any(marker in lowered for marker in _CODEX_RETRYABLE_ERROR_MARKERS)


def _build_codex_retry_messages(messages: List[Dict[str, Any]], partial_content: str) -> List[Dict[str, Any]]:
    retry_messages = [dict(message) for message in messages]
    partial_tail = (partial_content or "").strip()[-1500:]
    if not partial_tail:
        return retry_messages
    retry_messages.append(
        {
            "role": "user",
            "content": (
                "직전 Codex 응답이 연결 문제로 중단되었습니다. "
                "아래 마지막 생성 부분을 반복하지 말고 바로 이어서 계속해주세요.\n\n"
                f"{partial_tail}"
            ),
        }
    )
    return retry_messages


async def _stream_codex_relay_once(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Codex CLI Relay (/codex-stream) 경유 GPT 스트리밍. ChatGPT Plus OAuth."""
    formatted = _format_messages_for_llm(messages, has_resume=False)
    if isinstance(formatted, list):
        formatted = "\n".join(b.get("text", "") for b in formatted if isinstance(b, dict))
    req_body = {
        "model": model,
        "system_prompt": system_prompt,
        "messages_text": formatted,
        "session_id": session_id or "",
        "project": "AADS",
        "tool_names": [t.get("name", "") for t in (tools or []) if t.get("name")],
        "tool_schemas": [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "params": list((t.get("input_schema", {}).get("properties", {})).keys()),
            }
            for t in (tools or [])
            if t.get("name")
        ],
    }
    display_model = _CODEX_MODEL_DISPLAY.get(model, model)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            try:
                hc = await client.get(f"{_CLAUDE_RELAY_URL}/health", timeout=5.0)
                if hc.status_code != 200:
                    yield {"type": "error", "content": f"Codex Relay not healthy: {hc.status_code}"}
                    return
            except Exception as hc_err:
                yield {"type": "error", "content": f"Codex Relay unreachable: {hc_err}"}
                return
            async with client.stream(
                "POST", f"{_CLAUDE_RELAY_URL}/codex-stream",
                json=req_body, timeout=httpx.Timeout(300.0, connect=10.0),
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield {"type": "error", "content": f"Codex Relay {resp.status_code}: {body.decode()[:200]}"}
                    return
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    if evt_type == "assistant" and event.get("subtype") == "text":
                        yield {"type": "delta", "content": event.get("text", "")}
                    elif evt_type == "tool_use":
                        yield {
                            "type": "tool_use",
                            "tool_name": event.get("tool_name", ""),
                            "tool_use_id": event.get("tool_use_id", ""),
                            "tool_input": event.get("tool_input", {}),
                        }
                    elif evt_type == "tool_result":
                        tool_event = {
                            "type": "tool_result",
                            "tool_name": event.get("tool_name", ""),
                            "tool_use_id": event.get("tool_use_id", ""),
                            "content": event.get("content", ""),
                            "is_error": bool(event.get("is_error")),
                            "error_type": event.get("error_type", ""),
                            "cancel_scope": event.get("cancel_scope", ""),
                            "raw_error": event.get("raw_error", "")[:500],
                        }
                        if not tool_event.get("error_type"):
                            tool_event.update(_classify_relay_tool_result(
                                tool_event.get("content", ""),
                                session_id=session_id,
                                relay_name="codex",
                                tool_name=tool_event.get("tool_name", ""),
                                raw_error=tool_event.get("raw_error", ""),
                            ))
                        if tool_event.get("is_error"):
                            logger.warning(
                                "codex_relay_tool_error: session=%s tool=%s error_type=%s content=%s",
                                (session_id or "default")[:8],
                                tool_event.get("tool_name", ""),
                                tool_event.get("error_type", "tool_error"),
                                str(tool_event.get("raw_error") or tool_event.get("content", ""))[:240],
                            )
                        yield tool_event
                    elif evt_type == "error":
                        yield {"type": "error", "content": event.get("content", "Codex error")}
                        return
                    elif evt_type == "result":
                        in_tok = event.get("input_tokens", 0)
                        out_tok = event.get("output_tokens", 0)
                        cost = _estimate_cost(model, in_tok, out_tok)
                        yield {"type": "done", "model": display_model, "cost": str(cost),
                               "input_tokens": in_tok, "output_tokens": out_tok}
    except httpx.ConnectError as e:
        yield {"type": "error", "content": f"Codex Relay connect failed: {e}"}
    except httpx.ReadTimeout:
        yield {"type": "error", "content": "Codex Relay timeout (300s)"}
    except Exception as e:
        logger.error(f"codex_relay_error: {e}")
        yield {"type": "error", "content": str(e)}


async def _stream_codex_relay(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    display_model = _CODEX_MODEL_DISPLAY.get(model, model)
    retry_messages = messages
    partial_content = ""

    yield {"type": "model_info", "model": display_model}

    for attempt_idx in range(len(_CODEX_RETRY_DELAYS) + 1):
        last_error: Optional[str] = None
        async for event in _stream_codex_relay_once(
            model,
            system_prompt,
            retry_messages,
            tools=tools,
            session_id=session_id,
        ):
            event_type = event.get("type")
            if event_type == "delta":
                partial_content += event.get("content", "")
                yield event
                continue
            if event_type == "error":
                last_error = str(event.get("content", "Codex error"))
                break
            yield event
            if event_type == "done":
                return

        if not last_error:
            return

        if attempt_idx >= len(_CODEX_RETRY_DELAYS) or not _is_codex_retryable_error(last_error):
            yield {"type": "error", "content": last_error}
            return

        retry_delay = _CODEX_RETRY_DELAYS[attempt_idx]
        logger.warning(
            "codex_retry_same_model: model=%s session=%s attempt=%s/%s error=%s",
            model,
            (session_id or "default")[:8],
            attempt_idx + 1,
            len(_CODEX_RETRY_DELAYS),
            last_error[:200],
        )
        yield {
            "type": "delta",
            "content": (
                f"\n\n⚠️ _{display_model} 연결이 일시 중단되어 "
                f"{retry_delay:.0f}초 후 동일 모델로 다시 이어갑니다 ({attempt_idx + 1}/{len(_CODEX_RETRY_DELAYS)})._"
                "\n\n"
            ),
        }
        await asyncio.sleep(retry_delay)
        retry_messages = _build_codex_retry_messages(messages, partial_content)


async def _stream_agent_sdk(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Claude Agent SDK를 통한 스트리밍 (토큰 자동 교대).

    Agent SDK → 번들 CLI → Anthropic API (OAuth 직접 인증).
    Naver 토큰 우선 → 실패 시 Gmail 토큰 폴백.
    MCP 브릿지로 55개 도구 사용. 세션 자동 관리.
    """
    sdk_model = _ANTHROPIC_MODEL_ID.get(model, model)

    # 세션 이어가기 여부에 따라 메시지 포맷 결정 (이미지 블록 보존)
    _has_resume = bool(_cli_session_map.get(session_id)) if session_id else False
    user_message: Union[str, List[Dict[str, Any]]] = _format_messages_for_llm(messages, has_resume=_has_resume)

    # Agent SDK는 CLI의 자체 OAuth 인증을 사용 (~/.claude/.credentials_account*.json)
    # API 키를 env로 전달하지 않음 — CLI가 OAuth 토큰을 자동 관리
    _RETRYABLE_PATTERNS = [
        "rate_limit", "429", "rate limit",
        "overloaded", "529", "503",
        "credit", "402",
        "exit code 1",
        "server_error", "500", "internal",
        "timeout", "connection",
    ]

    yield {"type": "model_info", "model": sdk_model}

    _MAX_RETRIES = 3
    _last_error = ""
    for _attempt in range(_MAX_RETRIES):
        error_msg = ""
        async for evt in _run_agent_sdk_with_key(
            "", sdk_model, system_prompt, user_message, session_id,
        ):
            if evt.get("type") == "error":
                error_msg = evt.get("content", "")
                break
            yield evt

        if not error_msg:
            return  # 성공

        _last_error = error_msg
        is_retryable = any(p in error_msg.lower() for p in _RETRYABLE_PATTERNS)

        if not is_retryable:
            logger.error(f"agent_sdk_fatal: attempt={_attempt+1} error={error_msg[:100]}")
            yield {"type": "error", "content": error_msg}
            return

        logger.warning(f"agent_sdk_retry: attempt={_attempt+1}/{_MAX_RETRIES} error={error_msg[:80]}")
        yield {"type": "heartbeat"}

        if _attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(min(2 ** _attempt, 4))

    yield {"type": "error", "content": f"Agent SDK failed after {_MAX_RETRIES} attempts: {_last_error[:100]}"}


async def _run_agent_sdk_with_key(
    api_key: str,
    sdk_model: str,
    system_prompt: str,
    user_message: Union[str, List[Dict[str, Any]]],
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """단일 API 키로 Agent SDK 실행. 세션 이어가기(--resume) 지원.

    user_message가 list이면 content block 배열 (이미지 포함) → SDK prompt에 그대로 전달.
    """
    from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions

    # MCP config: 컨테이너 내부에서 직접 브릿지 실행
    _mcp_cfg = json.dumps({
        "mcpServers": {
            "aads-tools": {
                "command": "python",
                "args": ["-m", "mcp_servers.aads_tools_bridge"],
                "cwd": "/app",
                "env": {"AADS_SESSION_ID": session_id or ""},
            }
        }
    })

    # 세션 이어가기: AADS session_id → CLI session_id 매핑
    cli_session_id = _cli_session_map.get(session_id) if session_id else None

    # Agent 팀 정의: 조사/개발/QA 서브에이전트
    from claude_agent_sdk import AgentDefinition
    _agents = {
        "researcher": AgentDefinition(
            description="코드 탐색, DB 조회, 로그 분석, 서버 상태 확인 등 조사가 필요할 때 사용. 여러 파일/DB를 병렬로 조사할 때 효율적.",
            prompt=(
                "당신은 시스템 조사 전문가입니다. "
                "MCP 도구(read_remote_file, query_db, query_project_database, search_logs, list_remote_dir, git_remote_status)를 사용하여 "
                "요청된 정보를 정확하게 수집하고 구조화된 보고서로 반환하세요. "
                "추측하지 말고 반드시 도구로 확인한 데이터만 보고하세요."
            ),
            model="sonnet",
        ),
        "developer": AgentDefinition(
            description="코드 수정, 파일 작성, 패치 적용, git 커밋/푸시 등 개발 작업이 필요할 때 사용.",
            prompt=(
                "당신은 풀스택 개발자입니다. "
                "MCP 도구(write_remote_file, patch_remote_file, run_remote_command, git_remote_add, git_remote_commit, git_remote_push)를 사용하여 "
                "요청된 코드 변경을 정확하게 수행하세요. "
                "변경 전 반드시 현재 코드를 read_remote_file로 확인하고, 변경 후 검증하세요."
            ),
            model="sonnet",
        ),
        "qa": AgentDefinition(
            description="테스트 실행, 변경사항 검증, 서비스 헬스체크, 에러 확인 등 품질 검증이 필요할 때 사용.",
            prompt=(
                "당신은 QA 엔지니어입니다. "
                "MCP 도구를 사용하여 시스템 상태를 검증하고, 에러를 탐지하고, 변경사항이 정상 반영되었는지 확인하세요. "
                "문제 발견 시 구체적인 에러 내용과 재현 경로를 보고하세요."
            ),
            model="sonnet",
        ),
    }

    # cli_path: OAuth 래퍼 스크립트 — 충돌 env unset + CLAUDE_CODE_OAUTH_TOKEN 설정
    # 래퍼가 번들 CLI를 실행하면서 충돌 env를 unset하고  # R-AUTH: 래퍼 설명
    # ANTHROPIC_AUTH_TOKEN을 CLAUDE_CODE_OAUTH_TOKEN으로 전달 + HOME 격리
    _cli_path = os.getenv("CLAUDE_CLI_PATH", "/app/scripts/claude-oauth-wrapper.sh")

    opts = ClaudeAgentOptions(
        model=sdk_model,
        max_turns=200,
        permission_mode="acceptEdits",
        cwd="/app",
        cli_path=_cli_path,
        system_prompt=system_prompt,
        mcp_servers=_mcp_cfg,
        agents=_agents,
        allowed_tools=["Agent", "mcp__aads-tools__*"],
        disallowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep",
                          "WebFetch", "WebSearch", "NotebookEdit"],
    )
    # --resume: 이전 대화가 있으면 이어가기
    if cli_session_id:
        opts.resume = cli_session_id
        logger.info(f"agent_sdk_resume: aads={session_id[:8]} cli={cli_session_id[:8]}")

    full_text = ""
    tools_called_list: List[str] = []
    _tool_id_to_name: Dict[str, str] = {}
    total_cost = 0.0
    in_tokens = 0
    out_tokens = 0
    _captured_cli_sid = cli_session_id or ""  # resume 시 기존 ID 유지

    try:
        async for msg in sdk_query(prompt=user_message, options=opts):
            msg_type = type(msg).__name__

            if msg_type == "AssistantMessage":
                # 에러 응답 감지 (CLI가 에러를 assistant 메시지로 반환하는 경우)
                for block in msg.content:
                    block_type = type(block).__name__
                    if block_type == "TextBlock":
                        text = block.text or ""
                        if text.startswith("API Error:") or "authentication_failed" in text:
                            yield {"type": "error", "content": text}
                            return
                        if text:
                            full_text += text
                            yield {"type": "delta", "content": text}
                    elif block_type == "ToolUseBlock":
                        raw_name = block.name or ""
                        tool_name = raw_name.split("__")[-1] if raw_name.startswith("mcp__") else raw_name
                        tool_id = block.id or ""
                        if tool_id and tool_name:
                            _tool_id_to_name[tool_id] = tool_name
                        tools_called_list.append(tool_name)
                        yield {
                            "type": "tool_use",
                            "tool_name": tool_name,
                            "tool_use_id": tool_id,
                            "tool_input": block.input if hasattr(block, "input") else {},
                        }
                    elif block_type == "ThinkingBlock":
                        thinking = getattr(block, "thinking", "")
                        if thinking:
                            yield {"type": "thinking", "thinking": thinking}

            elif msg_type == "UserMessage":
                content = msg.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_id = block.get("tool_use_id", "")
                            tool_name = _tool_id_to_name.get(tool_id, "")
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_content = "\n".join(
                                    b.get("text", "") for b in result_content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            yield {
                                "type": "tool_result",
                                "tool_name": tool_name,
                                "tool_use_id": tool_id,
                                "content": str(result_content)[:3000],
                            }
                yield {"type": "heartbeat"}

            elif msg_type == "SystemMessage":
                # init 이벤트에서 CLI session_id 캡처
                data = getattr(msg, "data", None) or {}
                if isinstance(data, dict) and data.get("session_id"):
                    _captured_cli_sid = data["session_id"]
                yield {"type": "heartbeat"}

            elif msg_type == "ResultMessage":
                is_error = getattr(msg, "is_error", False)
                if is_error:
                    yield {"type": "error", "content": getattr(msg, "result", "SDK error")}
                    return
                total_cost = getattr(msg, "total_cost_usd", 0) or 0
                usage = getattr(msg, "usage", {}) or {}
                in_tokens = usage.get("input_tokens", 0) or 0
                out_tokens = usage.get("output_tokens", 0) or 0
                # ResultMessage에서도 session_id 캡처 (폴백)
                result_sid = getattr(msg, "session_id", None)
                if result_sid:
                    _captured_cli_sid = result_sid

    except Exception as e:
        error_str = str(e)
        logger.error(f"agent_sdk_key_error: {error_str[:200]}")
        yield {"type": "error", "content": error_str}
        return

    # 세션 매핑 저장 (대화 이어가기용)
    if session_id and _captured_cli_sid:
        _cli_session_map[session_id] = _captured_cli_sid
        logger.info(f"agent_sdk_session_map: aads={session_id[:8]} -> cli={_captured_cli_sid[:8]}")

    # done 이벤트 + 사용량 DB 기록
    cost = total_cost if total_cost else float(_estimate_cost("claude-sonnet", in_tokens, out_tokens))
    # Agent SDK 경로: 헤더 없지만 토큰 사용량은 기록
    _sdk_tokens = _ap_get_tokens()
    _sdk_token = _sdk_tokens[0] if _sdk_tokens else ""
    _log_oauth_usage(
        token=_sdk_token, model=sdk_model,
        input_tokens=in_tokens, output_tokens=out_tokens,
        cost_usd=cost,
        call_source="model_selector_sdk",
        session_id=session_id or "",
    )
    yield {
        "type": "done",
        "model": sdk_model,
        "cost": str(round(cost, 6)),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "tools_called": tools_called_list,
    }


def _format_messages_as_text(messages: List[Dict[str, Any]], has_resume: bool = False) -> str:
    """메시지 배열 → 텍스트 변환.

    has_resume=True: CLI가 이전 대화를 기억하므로 최신 user 메시지만 전달.
    has_resume=False: 대화 기록 포함 (최근 40개 메시지, CLI에 컨텍스트 제공).
    """

    def _extract_text(content) -> str:
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        tc = block.get("content", "")
                        if isinstance(tc, str):
                            parts.append("[도구결과] %s" % tc[:500])
                        elif isinstance(tc, list):
                            for b in tc:
                                if isinstance(b, dict) and b.get("type") == "text":
                                    parts.append("[도구결과] %s" % b.get("text", "")[:500])
                    elif block.get("type") == "tool_use":
                        parts.append("[도구호출: %s]" % block.get("name", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        elif isinstance(content, str):
            return content
        return str(content)

    # --resume 있으면: 최신 user 메시지만
    if has_resume:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                text = _extract_text(msg.get("content", ""))
                if text.strip():
                    return text

    # --resume 없으면: 최근 대화 기록 포함 (최대 40개 메시지)
    recent = messages[-40:] if len(messages) > 40 else messages
    parts = []
    for msg in recent:
        role = msg.get("role", "user")
        if role == "system":
            continue
        content = _extract_text(msg.get("content", ""))
        if not content.strip():
            continue
        # 긴 응답은 축소
        if role == "assistant" and len(content) > 1000:
            content = content[:800] + "\n...[응답 축소]..." + content[-200:]
        role_label = "CEO" if role == "user" else "AI"
        parts.append("[%s]\n%s" % (role_label, content))
    return "\n\n".join(parts)


def _format_messages_for_llm(
    messages: List[Dict[str, Any]], has_resume: bool = False
) -> Union[str, List[Dict[str, Any]]]:
    """메시지 배열 → 텍스트 또는 content block 배열 변환 (이미지 블록 보존).

    이미지가 없으면 str 반환 (기존 동작 유지),
    이미지가 있으면 Anthropic content block 리스트 반환.
    """
    # 최신 user 메시지에서 이미지 블록 존재 여부 확인
    latest_user_msg = None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            latest_user_msg = msg
            break

    if not latest_user_msg:
        return _format_messages_as_text(messages, has_resume)

    content = latest_user_msg.get("content", "")
    has_images = (
        isinstance(content, list)
        and any(
            isinstance(b, dict) and b.get("type") == "image"
            for b in content
        )
    )

    if not has_images:
        return _format_messages_as_text(messages, has_resume)

    # --- 이미지 포함: content block 배열 구성 ---
    def _content_to_blocks(c) -> List[Dict[str, Any]]:
        """content → text/image 블록 리스트 (도구 결과는 텍스트 축약)."""
        if isinstance(c, str):
            return [{"type": "text", "text": c}]
        if not isinstance(c, list):
            return [{"type": "text", "text": str(c)}]
        blocks: List[Dict[str, Any]] = []
        for b in c:
            if isinstance(b, dict):
                if b.get("type") in ("text", "image"):
                    blocks.append(b)
                elif b.get("type") == "tool_result":
                    tc = b.get("content", "")
                    if isinstance(tc, str):
                        blocks.append({"type": "text", "text": "[도구결과] %s" % tc[:500]})
                    elif isinstance(tc, list):
                        for tb in tc:
                            if isinstance(tb, dict) and tb.get("type") == "text":
                                blocks.append({"type": "text", "text": "[도구결과] %s" % tb.get("text", "")[:500]})
                elif b.get("type") == "tool_use":
                    blocks.append({"type": "text", "text": "[도구호출: %s]" % b.get("name", "")})
            elif isinstance(b, str):
                blocks.append({"type": "text", "text": b})
        return blocks

    # has_resume=True: 최신 user 메시지만 (이미지 포함)
    if has_resume:
        return _content_to_blocks(content)

    # has_resume=False: 대화 이력(텍스트) + 최신 메시지(이미지 포함)
    blocks: List[Dict[str, Any]] = []

    # 이전 대화를 텍스트로 축약 (최신 user 제외)
    history_msgs = [m for m in messages if m is not latest_user_msg]
    if history_msgs:
        history_text = _format_messages_as_text(history_msgs, has_resume=False)
        if history_text.strip():
            blocks.append({"type": "text", "text": history_text + "\n\n[CEO]"})

    # 최신 user 메시지의 content blocks (이미지 보존)
    blocks.extend(_content_to_blocks(content))
    return blocks


def _classify_relay_tool_result(
    content: Any,
    session_id: Optional[str] = None,
    relay_name: str = "claude",
    tool_name: str = "",
    raw_error: str = "",
) -> Dict[str, Any]:
    text = str(raw_error or content or "").strip()
    lowered = text.lower()
    if (
        "session cancelled mcp tool call" in lowered
        or "user cancelled mcp tool call" in lowered
        or "user canceled mcp tool call" in lowered
        or ('"error"' in lowered and '"cancelled"' in lowered)
    ):
        return {
            "is_error": True,
            "error_type": "session_cancelled_mcp_tool_call",
            "cancel_scope": "session",
            "raw_error": text[:500],
            "content": (
                "session cancelled MCP tool call "
                f"(relay={relay_name}, session={(session_id or 'default')[:8]}, tool={tool_name or 'unknown'})"
            ),
        }
    return {}


def _map_cli_event(event: dict, session_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Claude CLI NDJSON 이벤트 → AADS SSE 이벤트 리스트로 변환.

    Returns None if event should be skipped, otherwise a list of AADS events.
    """
    evt_type = event.get("type", "")

    # init 이벤트 — 스킵
    if evt_type == "system" and event.get("subtype") == "init":
        return None

    # assistant 메시지 — 텍스트/도구 추출
    if evt_type == "assistant":
        msg = event.get("message", {})
        content_blocks = msg.get("content", [])
        events = []
        for block in content_blocks:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        events.append({"type": "delta", "content": text})
                elif block.get("type") == "tool_use":
                    # MCP 접두사 제거: mcp__aads-tools__query_db → query_db
                    raw_name = block.get("name", "")
                    tool_name = raw_name.split("__")[-1] if raw_name.startswith("mcp__") else raw_name
                    events.append({
                        "type": "tool_use",
                        "tool_name": tool_name,
                        "tool_use_id": block.get("id", ""),
                        "tool_input": block.get("input", {}),
                    })
        # usage 정보 (부분)
        usage = msg.get("usage", {})
        if usage.get("input_tokens") or usage.get("output_tokens"):
            # heartbeat로 진행 표시
            events.append({"type": "heartbeat"})
        return events if events else None

    # user 이벤트 — MCP 도구 결과 (CLI가 tool_result를 user 메시지로 감싸서 전달)
    if evt_type == "user":
        msg = event.get("message", {})
        content_blocks = msg.get("content", [])
        events = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # tool_result 내부의 텍스트 추출
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = "\n".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                tool_use_id = block.get("tool_use_id", "")
                tool_event = {
                    "type": "tool_result",
                    "tool_name": "",  # _stream_claude_cli에서 복원
                    "tool_use_id": tool_use_id,
                    "content": str(result_content)[:3000],
                    "is_error": bool(block.get("is_error")),
                    "error_type": block.get("aads_error_type", ""),
                    "cancel_scope": block.get("aads_cancel_scope", ""),
                    "raw_error": block.get("aads_raw_error", "")[:500],
                }
                if not tool_event.get("error_type"):
                    tool_event.update(_classify_relay_tool_result(
                        tool_event.get("content", ""),
                        session_id=session_id,
                        relay_name="claude",
                    ))
                events.append(tool_event)
        return events if events else None

    # tool_result 이벤트 (직접 형식 — 폴백)
    if evt_type == "tool_result":
        tool_name = event.get("tool_name", "")
        content = event.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        payload = {
            "type": "tool_result",
            "tool_name": tool_name,
            "tool_use_id": event.get("tool_use_id", ""),
            "content": str(content)[:3000],
            "is_error": bool(event.get("is_error")),
            "error_type": event.get("error_type", ""),
            "cancel_scope": event.get("cancel_scope", ""),
            "raw_error": event.get("raw_error", "")[:500],
        }
        if not payload.get("error_type"):
            payload.update(_classify_relay_tool_result(
                payload.get("content", ""),
                session_id=session_id,
                relay_name="claude",
                tool_name=tool_name,
                raw_error=payload.get("raw_error", ""),
            ))
        return [payload]

    # result 이벤트 — 최종 완료
    if evt_type == "result":
        usage = event.get("usage", {})
        model = ""
        # modelUsage에서 모델명과 토큰 추출
        model_usage = event.get("modelUsage", {})
        in_tokens = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        total_cost = event.get("total_cost_usd", 0)
        for m, mu in model_usage.items():
            model = m.split("[")[0]  # "claude-opus-4-6[1m]" → "claude-opus-4-6"
            in_tokens = mu.get("inputTokens", in_tokens)
            out_tokens = mu.get("outputTokens", out_tokens)
            if mu.get("costUSD"):
                total_cost = mu["costUSD"]
            break

        # result 텍스트 (CLI가 최종 결과를 result 필드에도 넣음)
        result_text = event.get("result", "")
        events = []
        # result에 텍스트가 있고 아직 delta로 안 보냈으면 보내기
        # (보통 assistant 이벤트에서 이미 보냄 — 중복 방지를 위해 스킵)
        events.append({
            "type": "done",
            "model": model,
            "cost": str(round(total_cost, 6)),
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
        })
        return events

    # 그 외: heartbeat
    return [{"type": "heartbeat"}]


def _trim_tool_loop_context(
    current_messages: list,
    current_turn: int,
    max_budget: int = 120_000,
) -> list:
    """도구 루프 중 컨텍스트 토큰 예산 관리.

    CEO 메시지(user role)는 절대 삭제하지 않음.
    도구 결과는 F5(tool_archive)에 원본 보관되므로 축소 안전.

    단계별 축소:
    - > max_budget(120K): 10턴 이전 tool_result를 1줄 요약으로 교체
    - > max_budget+30K(150K): 15턴 이전 assistant 500자 절삭
    - > max_budget+50K(170K): 최근 20턴만 유지, 나머지 드롭 + 요약 메시지
    """
    # Estimate total tokens in current_messages
    total_text = ""
    for m in current_messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_text += str(block.get("content", "")) + str(block.get("text", ""))
                else:
                    total_text += str(block)
        elif isinstance(content, str):
            total_text += content

    est_tokens = len(total_text.encode("utf-8")) // 3

    if est_tokens <= max_budget:
        return current_messages

    logger.warning(f"trim_tool_loop: {est_tokens:,} tokens > {max_budget:,}, trimming (turn={current_turn})")

    result = list(current_messages)

    # Phase 1: > 120K — Replace old tool_results with placeholders
    if est_tokens > max_budget:
        cutoff = max(0, len(result) - current_turn * 2 - 20)  # ~10 turns back roughly
        for i in range(cutoff):
            m = result[i]
            content = m.get("content", "")
            if m.get("role") == "user" and isinstance(content, list):
                # tool_result blocks
                new_blocks = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id", "")
                        new_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "[이전 도구 결과 축소됨. 원본은 tool_archive에 보관. 필요시 도구 재호출.]",
                        })
                    else:
                        new_blocks.append(block)
                result[i] = {**m, "content": new_blocks}
            elif m.get("role") == "user" and isinstance(content, str) and "[시스템 도구 조회 결과" in content:
                # Compressed tool result string
                result[i] = {**m, "content": "[이전 도구 결과 축소됨. 필요시 재호출.]"}

    # Re-estimate after Phase 1
    total_text2 = ""
    for m in result:
        c = m.get("content", "")
        if isinstance(c, list):
            for b in c:
                total_text2 += str(b.get("content", "")) + str(b.get("text", "")) if isinstance(b, dict) else str(b)
        elif isinstance(c, str):
            total_text2 += c
    est2 = len(total_text2.encode("utf-8")) // 3

    # Phase 2: > 150K — Truncate old assistant messages
    if est2 > max_budget + 30_000:
        cutoff2 = max(0, len(result) - 30)  # Keep last 15 turns (30 messages)
        for i in range(cutoff2):
            m = result[i]
            if m.get("role") == "assistant":
                content = m.get("content", "")
                if isinstance(content, str) and len(content) > 500:
                    result[i] = {**m, "content": content[:500] + "\n\n[...응답 축소됨...]"}
                elif isinstance(content, list):
                    new_blocks = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if len(text) > 500:
                                new_blocks.append({**block, "text": text[:500] + "\n\n[...응답 축소됨...]"})
                            else:
                                new_blocks.append(block)
                        else:
                            new_blocks.append(block)
                    result[i] = {**m, "content": new_blocks}

    # Phase 3: > 170K — Keep only last 20 turns
    total_text3 = ""
    for m in result:
        c = m.get("content", "")
        if isinstance(c, list):
            for b in c:
                total_text3 += str(b.get("content", "")) + str(b.get("text", "")) if isinstance(b, dict) else str(b)
        elif isinstance(c, str):
            total_text3 += c
    est3 = len(total_text3.encode("utf-8")) // 3

    if est3 > max_budget + 50_000:
        keep_count = 40  # ~20 turns
        if len(result) > keep_count:
            dropped = len(result) - keep_count
            summary_msg = {
                "role": "user",
                "content": f"[이전 {dropped}개 메시지 생략됨. 핵심 사항은 memory_facts에 보존됨. 최근 대화만 표시됩니다.]",
            }
            result = [summary_msg] + result[-keep_count:]
            logger.warning(f"trim_tool_loop_emergency: dropped {dropped} messages, keeping {keep_count}")

    return result


async def _stream_anthropic(
    intent_result: IntentResult,
    model_alias: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Anthropic SDK 직접 스트리밍 (Tool Use + Extended Thinking + Prompt Caching)."""
    global _anthropic
    model_id = _ANTHROPIC_MODEL_ID.get(model_alias, "claude-sonnet-4-6")

    # AADS-186E-2: Extended Thinking — Opus/Sonnet 4.6 Adaptive Thinking
    use_thinking = (
        _EXTENDED_THINKING_ENABLED
        and intent_result.use_extended_thinking
        and model_alias in ("claude-opus", "claude-sonnet")
    )
    max_tokens = _MAX_TOKENS_CLAUDE_THINKING if use_thinking else _MAX_TOKENS_CLAUDE

    # 시스템 프롬프트 (Prompt Caching: Layer 1 정적 부분에 cache_control)
    system_blocks = _build_system_with_cache(system_prompt)

    # Adaptive Thinking (4.7 호환) — 모델이 자동으로 사고 깊이 결정
    thinking_config = None
    _output_config = None
    if use_thinking:
        thinking_config = {"type": "adaptive", "display": "summarized"}
        _output_config = {"effort": "xhigh"}  # low/medium/high/xhigh/max

    full_text = ""
    thinking_text = ""
    input_tokens = 0
    output_tokens = 0

    # Tool Use 루프 — 절대 상한 + wall-clock 타임아웃
    _MAX_TOOL_TURNS = int(os.getenv("MAX_TOOL_TURNS", "500"))
    _TOOL_TURN_EXTEND = 50  # 자동 연장 턴
    _WALL_CLOCK_TIMEOUT = int(os.getenv("TOOL_LOOP_TIMEOUT_SEC", "1800"))  # 30분 절대 타임아웃
    _wall_clock_start = __import__("time").monotonic()
    # 방어: messages에서 role="system" 필터링 — Claude API는 system을 top-level 파라미터로만 허용
    current_messages = [m for m in messages if m.get("role") != "system"]
    # 방어: 마지막 메시지가 user가 아니면 Claude API "must end with user message" 에러 방지
    if current_messages and current_messages[-1].get("role") != "user":
        current_messages.append({"role": "user", "content": "계속해주세요."})
    tool_calls_made = []
    _consecutive_yellow = 0  # Yellow 등급 도구 연속 호출 카운터
    _consecutive_errors = 0  # 도구 연속 에러 카운터
    _total_errors = 0  # 도구 총 에러 수
    _same_tool_error_count: Dict[str, int] = {}  # 도구별 에러 횟수 (같은 도구 반복 실패 감지)
    # Green(읽기) 도구: 관대한 제한 — 대용량 파일 탐색 시 연속 호출 정상
    _GREEN_TOOLS = {
        "read_remote_file", "list_remote_dir", "read_github_file", "read_task_logs",
        "query_database", "query_project_database", "recall_notes", "search_chat_history",
        "check_task_status", "pipeline_c_status", "dashboard_query", "capture_screenshot",
    }
    _CONSECUTIVE_ERROR_WARN = 10   # 연속 10회 에러 시 경고 주입 (차단 없음)
    _CONSECUTIVE_ERROR_STOP = 999  # 사실상 비활성 — 연속 에러로 중단하지 않음
    _TOTAL_ERROR_STOP = 999        # 사실상 비활성 — 총 에러로 중단하지 않음
    _SAME_TOOL_ERROR_LIMIT = 10    # Yellow 도구: 10회 연속 에러 시 경고 (차단 아님)
    _SAME_TOOL_ERROR_LIMIT_GREEN = 999  # Green 도구: 제한 없음
    _YELLOW_TOOLS = {
        "write_remote_file", "patch_remote_file", "run_remote_command",
        "git_remote_add", "git_remote_commit", "git_remote_push",
        "git_remote_create_branch", "deep_crawl", "deep_research",
        "spawn_subagent", "spawn_parallel_subagents",
    }
    _YELLOW_CONSECUTIVE_LIMIT = 100  # 전체 턴 상한(100)과 동일 — 사실상 무제한
    _effective_max_turns = _MAX_TOOL_TURNS
    _turn = 0

    # 스트리밍 시작 시 모델 정보 전송 (프론트에서 대답 중 모델명 표시용)
    _display_model = _ANTHROPIC_MODEL_ID.get(model_alias, model_alias)
    yield {"type": "model_info", "model": _display_model}

    while _turn < _effective_max_turns:
        # Wall-clock 타임아웃 체크 (30분 기본)
        _elapsed = __import__("time").monotonic() - _wall_clock_start
        if _elapsed > _WALL_CLOCK_TIMEOUT:
            logger.warning(f"wall_clock_timeout: {_elapsed:.0f}s > {_WALL_CLOCK_TIMEOUT}s, forcing stop at turn {_turn}")
            yield {"type": "timeout", "content": f"⏱ 응답 시간 초과 ({_elapsed/60:.0f}분). 현재까지 결과를 반환합니다."}
            break

        api_kwargs: Dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": current_messages,
        }
        if tools:
            # 도구 정의 캐싱 (AADS-190: Prompt Caching 적용)
            try:
                from app.core.cache_config import build_cached_tools
                api_kwargs["tools"] = build_cached_tools(tools)
            except Exception:
                api_kwargs["tools"] = tools
            # Layer ③: 인텐트별 동적 tool_choice (AADS-188C Phase 3 + Priority)
            # force_any: 반드시 도구 호출해야 하는 인텐트 (데이터 조회 필수)
            # auto: 도구 사용 여부를 AI가 판단 (대부분 인텐트)
            # 생략: 도구 불필요 인텐트
            _intent = intent_result.intent
            if _turn == 0:
                _force_tool_intents = (
                    # Tier 1: 데이터 조회 필수
                    "status_check", "dashboard", "task_query", "task_history",
                    "health_check", "all_service_status", "cost_report",
                    # Tier 2: 분석 필수
                    "service_inspection", "code_explorer", "analyze_changes",
                    # Tier 4: 검색 필수
                    "search", "url_read",
                    # Tier 6: 브라우저 명시 요청
                    "browser",
                )
                _no_tool_intents = ("greeting", "casual")
                _auto_tool_intents = (
                    # AI가 도구 필요 여부 판단하는 인텐트
                    "cto_code_analysis", "cto_strategy", "cto_verify", "cto_impact",
                    "code_task", "directive", "directive_gen", "cto_directive",
                    "complex_analysis", "strategy", "planning", "decision",
                    "architect", "design", "design_fix", "cto_tech_debt",
                    "execute", "code_modify", "server_file",
                )
                if _intent in _force_tool_intents and intent_result.use_tools:
                    api_kwargs["tool_choice"] = {"type": "any"}
                elif _intent in _no_tool_intents:
                    pass  # tool_choice 생략
                elif _intent in _auto_tool_intents and intent_result.use_tools:
                    api_kwargs["tool_choice"] = {"type": "auto"}
                elif intent_result.use_tools:
                    api_kwargs["tool_choice"] = {"type": "auto"}
        # Beta features — extra_headers로 전달 (SDK 0.84+에서 betas 직접 파라미터 미지원)
        _betas = []
        if thinking_config:
            api_kwargs["thinking"] = thinking_config
            if _output_config:
                api_kwargs["output_config"] = _output_config
            _betas.append("interleaved-thinking-2025-05-14")
        if _betas:
            api_kwargs["extra_headers"] = {
                **(api_kwargs.get("extra_headers") or {}),
                "anthropic-beta": ",".join(_betas),
            }
        # Extended Thinking + tool_choice="any" 비호환 — auto로 복귀
        if thinking_config and "tool_choice" in api_kwargs:
            del api_kwargs["tool_choice"]

        # 디버그: API 호출 전 payload 요약 로깅
        _n_msgs = len(api_kwargs.get("messages", []))
        _n_tools = len(api_kwargs.get("tools", []) or [])
        _has_thinking = "thinking" in api_kwargs
        _tool_choice = api_kwargs.get("tool_choice", {}).get("type", "none")
        if _turn == 0:
            logger.info(f"anthropic_api_call: model={model_id} msgs={_n_msgs} tools={_n_tools} thinking={_has_thinking} tool_choice={_tool_choice} turn={_turn}")
            # 메시지 role 시퀀스 검증
            _roles = [m.get("role") for m in api_kwargs.get("messages", [])]
            if _roles:
                logger.info(f"anthropic_msg_roles: {_roles[:20]}{'...' if len(_roles)>20 else ''}")
                # content 타입 검증
                for _mi, _m in enumerate(api_kwargs.get("messages", [])[:5]):
                    _ct = type(_m.get("content")).__name__
                    _preview = str(_m.get("content", ""))[:80] if isinstance(_m.get("content"), str) else f"[{_ct}] len={len(_m.get('content', []))}" if isinstance(_m.get("content"), list) else str(type(_m.get("content")))
                    logger.info(f"anthropic_msg[{_mi}]: role={_m.get('role')} content_type={_ct} preview={_preview}")

        # 재시도 로직: 일시적 에러(400/429/529/503/네트워크)는 최대 10회 재시도
        # 400: Anthropic API 간헐적 invalid_request_error (2026-03 발생, ~50% 실패율)
        _RETRYABLE_STATUS = {400, 403, 429, 503, 529}
        _MAX_RETRIES = 10
        _retry_attempt = 0
        _last_error = None
        final_msg = None

        while _retry_attempt <= _MAX_RETRIES:
            try:
                async with _anthropic.messages.stream(**api_kwargs) as stream:
                    async for event in stream:
                        event_type = type(event).__name__

                        if event_type == "RawContentBlockDeltaEvent":
                            delta = event.delta
                            delta_type = type(delta).__name__
                            if delta_type == "TextDelta":
                                full_text += delta.text
                                yield {"type": "delta", "content": delta.text}
                            elif delta_type == "ThinkingDelta":
                                thinking_text += delta.thinking
                                yield {"type": "thinking", "thinking": delta.thinking}
                            elif delta_type == "InputJsonDelta":
                                pass  # tool_use 입력 JSON 누적 (스트림 종료 후 처리)

                    final_msg = await stream.get_final_message()
                break  # 성공 → 재시도 루프 탈출

            except (RateLimitError, APIConnectionError) as e:
                _retry_attempt += 1
                _last_error = e
                _status = getattr(e, 'status_code', 0)
                if _quota_class_http_error(_status, e) and _switch_oat_token():
                    api_kwargs["_client_refreshed"] = True
                    logger.warning("oat_switch_on_rate_limit: rotated OAuth / direct client, retrying")
                    _retry_attempt -= 1  # 스위치 후 재시도 카운트 차감
                if _retry_attempt <= _MAX_RETRIES:
                    _wait = min(2 ** _retry_attempt, 10)
                    logger.warning(f"claude_retry: attempt {_retry_attempt}/{_MAX_RETRIES}, status={_status}, wait={_wait}s, error={str(e)[:100]}")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                else:
                    logger.error(f"claude_retry_exhausted: {_MAX_RETRIES} retries failed, status={_status}, error={str(e)[:100]}")
                    yield {"type": "error", "content": str(e)}
                    return

            except APIStatusError as e:
                _retry_attempt += 1
                _last_error = e
                _status = getattr(e, 'status_code', 0)
                if _quota_class_http_error(_status, e) and _switch_oat_token():
                    logger.warning("oat_switch_on_quota_class: status=%s, rotated OAuth / direct client", _status)
                    _retry_attempt -= 1
                if _status in _RETRYABLE_STATUS and _retry_attempt <= _MAX_RETRIES:
                    # 400: 간헐적 에러 → 짧은 대기, 429/503: rate limit → 지수 백오프
                    _wait = 0.3 if _status == 400 else min(2 ** _retry_attempt, 10)
                    logger.warning(f"claude_retry: attempt {_retry_attempt}/{_MAX_RETRIES}, status={_status}, wait={_wait}s")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                elif _status in (402, 403) or _quota_class_http_error(_status, e):
                    _wait = 2
                    logger.warning(f"claude_retry_quota: attempt {_retry_attempt}, status={_status}, wait={_wait}s")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                else:
                    _err_body = getattr(e, 'body', None) or getattr(e, 'response', None)
                    logger.error(f"model_selector anthropic error: status={_status}, error={e}, body={str(_err_body)[:500]}, model={model_id}, msgs={len(current_messages)}, tools={len(tools or [])}")
                    yield {"type": "error", "content": str(e)}
                    return

            except Exception as e:
                _retry_attempt += 1
                _last_error = e
                if _retry_attempt <= _MAX_RETRIES:
                    _wait = min(2 ** _retry_attempt, 10)
                    logger.warning(f"claude_retry_unexpected: attempt {_retry_attempt}/{_MAX_RETRIES}, wait={_wait}s, error={str(e)[:80]}")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                    full_text = ""  # 부분 응답 리셋
                else:
                    logger.error(f"model_selector anthropic unexpected after {_MAX_RETRIES} retries: {e}")
                    yield {"type": "error", "content": str(e)}
                    return

        if final_msg is None:
            logger.error(f"model_selector: final_msg is None after retries, last_error={_last_error}")
            yield {"type": "error", "content": f"Claude API 응답 없음 (재시도 {_MAX_RETRIES}회 소진)"}
            return

        input_tokens += final_msg.usage.input_tokens
        output_tokens += final_msg.usage.output_tokens
        # Prompt Caching 히트율 로깅
        _cache_read = getattr(final_msg.usage, 'cache_read_input_tokens', 0) or 0
        _cache_create = getattr(final_msg.usage, 'cache_creation_input_tokens', 0) or 0
        if _cache_read or _cache_create:
            logger.info(f"prompt_cache: read={_cache_read} create={_cache_create} input={input_tokens} turn={_turn}")

        # Tool Use 처리
        tool_use_blocks = [b for b in final_msg.content if b.type == "tool_use"]
        if not tool_use_blocks:
            # 빈 응답 자동 재시도: 도구가 있는데 사용 안 하고 너무 짧은 응답 → 도구 강제 호출
            if _turn == 0 and tools and len(full_text) < 100 and intent_result.use_tools:
                logger.warning(f"empty_response_retry: '{full_text[:50]}' ({len(full_text)} chars), forcing tool_choice=any")
                # 응답 리셋 후 tool_choice=any로 재시도
                full_text = ""
                yield {"type": "delta", "content": ""}  # 프론트 스트림 리셋 신호
                api_kwargs["tool_choice"] = {"type": "any"}
                if "thinking" in api_kwargs:
                    del api_kwargs["tool_choice"]  # thinking과 tool_choice=any 비호환
                _turn += 1
                continue  # while 루프 재시도
            break  # 정상 종료

        # 도구 실행 (heartbeat 포함 — SSE 연결 유지)
        from app.services.tool_executor import ToolExecutor, current_chat_session_id as _cv_sid
        executor = ToolExecutor()
        if any(tu.name.startswith("pipeline_c") for tu in tool_use_blocks):
            logger.info(f"[DIAG] call_stream tool exec: ContextVar session_id='{_cv_sid.get('')}'")

        tool_results = []
        for tu in tool_use_blocks:
            # Yellow 도구 연속 실행 제한 체크
            if tu.name in _YELLOW_TOOLS:
                _consecutive_yellow += 1
            else:
                _consecutive_yellow = 0

            if _consecutive_yellow >= _YELLOW_CONSECUTIVE_LIMIT:
                logger.warning(f"yellow_consecutive_limit: {_consecutive_yellow} calls, pausing for CEO confirm")
                yield {
                    "type": "yellow_limit",
                    "content": f"쓰기 도구가 연속 {_consecutive_yellow}회 호출되었습니다. 계속 진행할까요?",
                    "tool_name": tu.name,
                    "consecutive_count": _consecutive_yellow,
                }
                # 제한 리셋 (경고 후 계속 진행 — 프론트에서 차단 UI 표시)
                _consecutive_yellow = 0

            yield {
                "type": "tool_use",
                "tool_name": tu.name,
                "tool_input": tu.input,
                "tool_use_id": tu.id,
            }
            tool_calls_made.append(tu.name)

            # 도구 실행 — 별도 asyncio.Task + Event 기반 heartbeat (P0-FIX: shield→Event 전환)
            # asyncio.shield 패턴 대신 Event + done_callback으로 도구 블로킹과 heartbeat 완전 분리
            _LONG_TOOLS = {"deep_research", "deep_crawl", "spawn_subagent", "spawn_parallel_subagents", "pipeline_c_execute"}
            _tool_timeout = 600 if tu.name in _LONG_TOOLS else 120
            _HB_TOOL_SEC = 8.0  # heartbeat 간격 (초)
            task = asyncio.create_task(executor.execute(tu.name, tu.input))
            _tool_start = __import__("time").monotonic()

            # Event 기반: 도구 완료 시 콜백으로 이벤트 설정 → heartbeat 루프 즉시 탈출
            _tool_done_evt = asyncio.Event()
            task.add_done_callback(lambda _: _tool_done_evt.set())

            while not _tool_done_evt.is_set():
                try:
                    await asyncio.wait_for(_tool_done_evt.wait(), timeout=_HB_TOOL_SEC)
                    break  # 도구 완료 신호 수신
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    _elapsed = __import__("time").monotonic() - _tool_start
                    if _elapsed > _tool_timeout:
                        task.cancel()
                        logger.warning(f"tool_timeout: {tu.name} exceeded {_tool_timeout}s, cancelled")
                        break
                except (asyncio.CancelledError, Exception):
                    break
            try:
                result_str = task.result() if task.done() and not task.cancelled() else json.dumps({"error": f"tool execution timeout ({_tool_timeout}s)", "tool": tu.name})
            except asyncio.CancelledError:
                result_str = json.dumps({"error": f"tool execution timeout ({_tool_timeout}s)", "tool": tu.name})
            except Exception as exc:
                logger.warning(f"tool execution error: tool={tu.name} error={exc}")
                result_str = json.dumps({"error": str(exc), "tool": tu.name})

            # 도구 결과 자동 압축 (컨텍스트에 넣기 전)
            try:
                from app.services.context_compressor import compress_tool_output
                compressed_str = compress_tool_output(tu.name, result_str)
            except Exception:
                compressed_str = result_str  # fallback: 원본 유지

            # 도구 에러 감지 — Green/Yellow 구분 카운팅
            _is_green = tu.name in _GREEN_TOOLS
            _is_tool_error = (
                (isinstance(result_str, str) and ("[ERROR]" in result_str or '"error"' in result_str[:50]))
                or (isinstance(result_str, dict) and "error" in result_str)
            )
            if _is_tool_error:
                _consecutive_errors += 1
                _total_errors += 1
                _same_tool_error_count[tu.name] = _same_tool_error_count.get(tu.name, 0) + 1
            else:
                _consecutive_errors = 0  # 성공하면 연속 에러 리셋
                _same_tool_error_count[tu.name] = 0  # 해당 도구 에러 카운트 리셋

            yield {
                "type": "tool_result",
                "tool_name": tu.name,
                "tool_use_id": tu.id,
                "content": result_str,  # 프론트에는 원본 전달
            }
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": compressed_str,  # 컨텍스트에는 압축본
            })

            # 같은 도구 반복 실패 감지 — 해당 도구만 차단 메시지 주입
            _same_err = _same_tool_error_count.get(tu.name, 0)
            _same_limit = _SAME_TOOL_ERROR_LIMIT_GREEN if _is_green else _SAME_TOOL_ERROR_LIMIT
            if _same_err >= _same_limit and _same_err % _same_limit == 0:
                # 경고만 주입 (차단하지 않음) — AI가 참고하여 전략 변경 유도
                tool_results[-1]["content"] = (
                    compressed_str + f"\n\n⚠️ {tu.name}이 {_same_err}회 연속 실패 중. "
                    "다른 파라미터나 다른 접근법도 검토해보세요."
                )
                logger.warning(f"same_tool_error_warn: {tu.name}={_same_err}, turn={_turn}")

            # 연속 에러 경고/중단 (Green 도구는 관대, Yellow 도구는 엄격)
            _eff_stop = _CONSECUTIVE_ERROR_STOP if not _is_green else _CONSECUTIVE_ERROR_STOP * 2
            _eff_warn = _CONSECUTIVE_ERROR_WARN if not _is_green else _CONSECUTIVE_ERROR_WARN * 2

            if _consecutive_errors >= _eff_stop or _total_errors >= _TOTAL_ERROR_STOP:
                _err_msg = (
                    f"⚠️ 도구 호출이 연속 {_consecutive_errors}회 실패했습니다 (총 {_total_errors}회). "
                    "다른 접근 방식을 시도하거나, 현재까지의 결과를 정리하여 응답하세요. "
                    "동일한 도구를 같은 방식으로 반복 호출하지 마세요."
                )
                # L-08: 중복 tool_use_id 방지 — 기존 결과 교체
                tool_results[-1]["content"] = _err_msg
                tool_results[-1]["is_error"] = True
                logger.warning(f"error_circuit_breaker: consecutive={_consecutive_errors}, total={_total_errors}, tool={tu.name}, turn={_turn}")
                break  # 이 턴의 남은 도구 실행 스킵
            elif _consecutive_errors >= _eff_warn:
                # 경고 주입 — AI가 다른 방식을 시도하도록 유도
                tool_results[-1]["content"] = (
                    compressed_str + f"\n\n⚠️ 연속 {_consecutive_errors}회 도구 에러. "
                    "같은 방식 재시도 금지. 다른 도구나 다른 파라미터를 시도하세요."
                )
            # F5: Tool Result Archive — 도구 결과 전문 보관 (백그라운드)
            try:
                from app.services.tool_archive import archive_tool_result as _archive
                _f5_sid = _cv_sid.get("") if _cv_sid else ""
                if _f5_sid:
                    from app.core.db_pool import get_pool as _get_pool_f5
                    _f5_pool = _get_pool_f5()
                    async with _f5_pool.acquire() as _f5c:
                        _f5_mid = await _f5c.fetchval(
                            "SELECT id::text FROM chat_messages WHERE session_id = $1::uuid AND role = 'user' ORDER BY created_at DESC LIMIT 1",
                            _f5_sid,
                        )
                    if _f5_mid:
                        asyncio.create_task(_archive(_f5_mid, tu.id, tu.name, dict(tu.input), result_str))
            except Exception:
                pass

        # 메시지에 AI 응답 + 도구 결과 추가
        # L-07 fix: SDK ContentBlock → API 허용 필드만 추출 (parsed_output 등 제거)
        _serialized = []
        for _blk in (final_msg.content if isinstance(final_msg.content, list) else [final_msg.content]):
            _blk_type = getattr(_blk, "type", None) or (isinstance(_blk, dict) and _blk.get("type")) or ""
            if _blk_type == "text":
                _serialized.append({"type": "text", "text": getattr(_blk, "text", "") or (_blk.get("text", "") if isinstance(_blk, dict) else str(_blk))})
            elif _blk_type == "tool_use":
                _serialized.append({
                    "type": "tool_use",
                    "id": getattr(_blk, "id", "") or (_blk.get("id", "") if isinstance(_blk, dict) else ""),
                    "name": getattr(_blk, "name", "") or (_blk.get("name", "") if isinstance(_blk, dict) else ""),
                    "input": getattr(_blk, "input", {}) or (_blk.get("input", {}) if isinstance(_blk, dict) else {}),
                })
            elif isinstance(_blk, dict):
                # 알 수 없는 타입은 허용 필드만 전달
                _clean = {k: v for k, v in _blk.items() if k in ("type", "text", "id", "name", "input", "content")}
                _serialized.append(_clean if _clean else {"type": "text", "text": str(_blk)})
            else:
                _serialized.append({"type": "text", "text": str(_blk)})
        current_messages = current_messages + [
            {"role": "assistant", "content": _serialized},
            {"role": "user", "content": tool_results},
        ]

        # Layer A: 도구 루프 토큰 예산 관리 (120K)
        current_messages = _trim_tool_loop_context(current_messages, _turn)

        # api_kwargs에 업데이트된 messages 반영 (참조가 끊어지므로 명시적 갱신)
        api_kwargs["messages"] = current_messages

        # CEO 인터럽트 체크: 도구 실행 완료 후, 다음 API 호출 전
        if session_id:
            from app.core.interrupt_queue import has_interrupt, pop_interrupts
            if has_interrupt(session_id):
                interrupts = pop_interrupts(session_id)
                interrupt_text = "\n".join(i["content"] for i in interrupts)
                # 인터럽트 첨부파일 → Claude Vision content blocks
                _intr_content: list | str = f"[CEO 추가 지시] 작업 도중 CEO가 새로운 지시를 보냈습니다. 현재까지의 작업 결과를 고려하고, 이 새 지시를 반영하여 다음 행동을 판단하세요. CEO 지시가 기존 작업과 충돌하면 CEO 지시를 우선합니다.\n\n{interrupt_text}"
                _intr_images = []
                for _intr_item in interrupts:
                    for att in _intr_item.get("attachments", []):
                        if att.get("type") == "image" and att.get("base64"):
                            _intr_images.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": att.get("media_type", "image/png"),
                                    "data": att["base64"],
                                },
                            })
                if _intr_images:
                    _intr_content = [{"type": "text", "text": _intr_content}] + _intr_images
                current_messages.append({"role": "user", "content": _intr_content})
                # 각 interrupt마다 개별 이벤트 yield (프론트 큐 동기화용)
                for _intr_item in interrupts:
                    yield {"type": "interrupt_applied", "content": _intr_item["content"][:100]}

        _turn += 1

        # 도구 턴 한도 도달 시 CEO 승인 요청 이벤트 발행 + 자동 연장
        if _turn >= _effective_max_turns and tool_use_blocks:
            logger.warning(f"tool_turn_limit: {_turn}/{_effective_max_turns} turns used, extending by {_TOOL_TURN_EXTEND}")
            _effective_max_turns += _TOOL_TURN_EXTEND
            # 무제한 — compaction이 컨텍스트 자동 관리
            yield {
                "type": "tool_turn_limit",
                "content": f"도구 호출이 {_turn}회에 도달했습니다. {_TOOL_TURN_EXTEND}턴 자동 연장합니다.",
                "current_turn": _turn,
                "extended_to": _effective_max_turns,
            }

    cost = _estimate_cost(model_alias, input_tokens, output_tokens)
    # 프론트 표시용: alias(claude-opus) → 실제 모델ID(claude-opus-4-6)
    _display_model = _ANTHROPIC_MODEL_ID.get(model_alias, model_alias)
    yield {
        "type": "done",
        "model": _display_model,
        "cost": str(cost),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_summary": thinking_text[:2000] if thinking_text else None,
        "tools_called": tool_calls_made,
    }


def _build_system_with_cache(system_prompt: str) -> List[Dict[str, Any]]:
    """
    시스템 프롬프트를 3-breakpoint로 분리하여 cache_control 적용.
    BP1: 정적 (role/rules) — 매 턴 동일, 캐시 효과 최대
    BP2: 준동적 (session/corrections) — 세션 내 안정적
    BP3: 동적 (preload/rag) — 매 턴 변경, 캐시 없음
    """
    _CC = {"type": "ephemeral"}
    blocks = []

    # BP1: 정적 파트 분리
    sep1 = "\n\n## 현재 상태"
    sep1_alt = "\n\n## 워크스페이스 추가 지시"
    idx1 = -1
    for sep in (sep1, sep1_alt):
        if sep in system_prompt:
            idx1 = system_prompt.index(sep)
            break

    if idx1 < 0:
        # 분리 불가 — 단일 블록
        return [{"type": "text", "text": system_prompt}]

    static_part = system_prompt[:idx1]
    rest = system_prompt[idx1:]

    blocks.append({"type": "text", "text": static_part, "cache_control": _CC})

    # BP2: 준동적 파트 분리 (<workspace_preload> 이전)
    sep2 = "<workspace_preload>"
    if sep2 in rest:
        idx2 = rest.index(sep2)
        semi_dynamic = rest[:idx2]
        dynamic = rest[idx2:]
        blocks.append({"type": "text", "text": semi_dynamic, "cache_control": _CC})
        blocks.append({"type": "text", "text": dynamic})
    else:
        # workspace_preload 없으면 2블록으로
        blocks.append({"type": "text", "text": rest})

    return blocks
