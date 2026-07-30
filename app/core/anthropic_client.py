"""
중앙 Anthropic 클라이언트 팩토리 + LiteLLM/DashScope 폴백.

OAuth 토큰으로 Anthropic API 직접 호출.
Claude 실패 시 Gemini 2.5 Flash (LiteLLM 경유)로 자동 폴백.
비Claude 모델(qwen-turbo 등)은 DashScope API 직접 또는 LiteLLM 프록시로 라우팅.
백그라운드 시스템(self_evaluator, fact_extractor, compaction 등)에서 사용.
"""
from __future__ import annotations

import asyncio
import os
import logging
import random
import time
from typing import Optional

import anthropic
import httpx
from anthropic import AsyncAnthropic

from app.core.auth_provider import (
    get_available_tokens, get_litellm_config,
    create_anthropic_client, mark_token_rate_limited,
)

logger = logging.getLogger(__name__)

_GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
# AADS P0(2026-07-26): Gemini prepaid 크레딧 고갈로 429 RESOURCE_EXHAUSTED 반복 발생.
# 폴백 체인에서 기본 제외하고 qwen3-235b를 다음 순위로 사용한다.
# 크레딧 충전 후 되살리려면 .env 에 LLM_GEMINI_FALLBACK_ENABLED=1 설정.
_GEMINI_FALLBACK_ENABLED = os.getenv("LLM_GEMINI_FALLBACK_ENABLED", "0").strip().lower() in ("1", "true", "yes")
_DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_DASHSCOPE_API_KEY = os.getenv("ALIBABA_API_KEY", "")
# AADS-LOOP P0(2026-07-30): Claude OAuth 토큰 만료(401) + Gemini 크레딧 고갈 +
# DashScope 404 동시 발생 시 배경 LLM이 전부 None을 반환해 루프/평가가 100% 실패했다.
# LiteLLM 경유 저비용 체인을 최종 폴백으로 둬 배경 작업 가용성을 유지한다.
_BG_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "LLM_BG_FALLBACK_MODELS", "groq-llama-70b,groq-gpt-oss-120b,qwen-flash"
    ).split(",")
    if m.strip()
]
# AADS-LOOP P0(2026-07-30): Claude OAuth 토큰 만료(401) + Gemini 크레딧 고갈 +
# DashScope 404 동시 발생 시 배경 LLM이 전부 None을 반환해 루프/평가가 100% 실패했다.
# LiteLLM 경유 저비용 체인을 최종 폴백으로 둬 배경 작업 가용성을 유지한다.
_BG_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "LLM_BG_FALLBACK_MODELS", "groq-llama-70b,groq-gpt-oss-120b,qwen-flash"
    ).split(",")
    if m.strip()
]
_CLAUDE_RETRY_BASE_SEC = 2.0
_CLAUDE_RETRY_MAX_DELAY_SEC = 30.0
_CLAUDE_RETRY_JITTER_SEC = 1.5
_CLAUDE_MAX_RETRIES = 60
_CLAUDE_RETRY_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
_CLAUDE_429_MAX_RETRIES = 30


def _retry_delay(attempt: int, status_code: int | None = None) -> float:
    """429: 고정 3초 (CEO 지시). 그 외: exponential backoff with jitter."""
    if status_code == 429:
        return 3.0
    base = _CLAUDE_RETRY_BASE_SEC
    delay = min(base * (2 ** min(attempt, 6)), _CLAUDE_RETRY_MAX_DELAY_SEC)
    return delay + random.uniform(0, _CLAUDE_RETRY_JITTER_SEC)

_bg_qwen_fail_streak: int = 0  # qwen-turbo 연속 실패 카운터 (AADS-204)


# ── LiteLLM 응답 래퍼 (Anthropic Message 호환) ──────────────────────

class _LiteLLMTextBlock:
    """Anthropic TextBlock 호환."""
    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class _LiteLLMUsage:
    """Anthropic Usage 호환."""
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _LiteLLMResponse:
    """LiteLLM/DashScope 응답을 Anthropic Message 형태로 래핑."""
    def __init__(self, text: str, model: str, usage_data: Optional[dict] = None):
        self.content = [_LiteLLMTextBlock(text)]
        self.model = model
        self.usage = _LiteLLMUsage(
            input_tokens=(usage_data or {}).get("prompt_tokens", 0),
            output_tokens=(usage_data or {}).get("completion_tokens", 0),
        )
        self.stop_reason = "end_turn"


# ── 공개 함수 ────────────────────────────────────────────────────────

def get_client(model_hint: str = "claude-haiku") -> AsyncAnthropic:
    """Anthropic API 직접 클라이언트 반환 (auth_provider 경유)."""
    return create_anthropic_client()


def _extract_status_code(exc: Exception) -> Optional[int]:
    """SDK/httpx 예외에서 HTTP 상태 코드를 최대한 보수적으로 추출."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status

    err_str = str(exc)
    for code in sorted(_CLAUDE_RETRY_STATUS_CODES):
        if str(code) in err_str:
            return code
    return None


def _is_timeout_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            asyncio.TimeoutError,
            httpx.ReadTimeout,
            httpx.TimeoutException,
            anthropic.APITimeoutError,
        ),
    ) or "timeout" in str(exc).lower()


def _is_retryable_error(exc: Exception) -> tuple[bool, Optional[int]]:
    status_code = _extract_status_code(exc)
    if _is_timeout_error(exc):
        return True, status_code
    return status_code in _CLAUDE_RETRY_STATUS_CODES, status_code


def _get_error_headers(exc: Exception) -> Optional[dict]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return dict(headers) if headers else None


async def call_llm_with_fallback(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    system: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Claude 호출 + 실패 시 Gemini 폴백. 백그라운드 평가/추출용.

    비Claude 모델(qwen-turbo 등) 지정 시 DashScope/LiteLLM으로 직접 라우팅.

    1순위: Claude moong76@gmail (slot:naver, PRIMARY)
    2순위: Claude moongoby@gmail (slot:gmail, FALLBACK)
    3순위: Gemini 2.5 Flash (LiteLLM 경유)

    Returns: 응답 텍스트 또는 None (전부 실패 시)
    """
    if tenant_id:
        from app.services.tenant_usage_limits import check_tenant_usage_limit

        await check_tenant_usage_limit(tenant_id, operation=f"llm:{model}", projected_calls=1)

    # 비Claude 모델 → DashScope/LiteLLM 직접
    if not model.startswith("claude"):
        try:
            if model.startswith("qwen"):
                return await _call_dashscope(prompt, model, max_tokens, system)
            return await _call_litellm(prompt, model, max_tokens, system)
        except Exception as e:
            logger.warning("litellm_bg_error: model=%s error=%s", model, str(e)[:80])
            # 실패 시 Gemini 폴백 — 기본 비활성(크레딧 고갈 429 반복 차단)
            if _GEMINI_FALLBACK_ENABLED:
                try:
                    return await _call_litellm(prompt, _GEMINI_FALLBACK_MODEL, max_tokens, system)
                except Exception as e2:
                    logger.warning("litellm_bg_gemini_fallback_error: %s", str(e2)[:80])
            for _fb_model in _BG_FALLBACK_MODELS:
                if _fb_model == model:
                    continue
                try:
                    _text = await _call_litellm(prompt, _fb_model, max_tokens, system)
                    if _text:
                        logger.info("bg_llm_last_resort_ok: model=%s", _fb_model)
                        return _text
                except Exception as e3:
                    logger.warning(
                        "bg_llm_last_resort_error: model=%s error=%s", _fb_model, str(e3)[:80]
                    )
            return None

    from app.services.oauth_usage_tracker import log_usage

    keys_to_try = get_available_tokens()
    for key in keys_to_try:
        last_error: Optional[Exception] = None
        _429_count = 0
        for retry_count in range(_CLAUDE_MAX_RETRIES + 1):
            t0 = time.monotonic()
            try:
                client = create_anthropic_client(token=key)
                msgs = [{"role": "user", "content": prompt}]
                kwargs = {"model": model, "max_tokens": max_tokens, "messages": msgs}
                if system:
                    kwargs["system"] = system
                raw = await client.messages.with_raw_response.create(**kwargs)
                resp = raw.parse()
                duration_ms = int((time.monotonic() - t0) * 1000)
                log_usage(
                    token=key,
                    model=model,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                    headers=raw.headers,
                    call_source="anthropic_client",
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                return resp.content[0].text
            except (httpx.ReadTimeout, anthropic.APITimeoutError) as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                log_usage(
                    token=key,
                    model=model,
                    call_source="anthropic_client",
                    error_code="timeout",
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                if retry_count < _CLAUDE_MAX_RETRIES:
                    wait = _retry_delay(retry_count, status_code=None)
                    logger.warning(
                        "claude_bg_retry_timeout: key=%s retry_count=%d/%d wait=%.1fs last_error=%s",
                        key[:12],
                        retry_count + 1,
                        _CLAUDE_MAX_RETRIES,
                        wait,
                        str(e)[:160],
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "claude_bg_timeout_exhausted: key=%s model=%s retry_count=%d last_error=%s",
                    key[:12],
                    model,
                    retry_count,
                    str(e)[:160],
                )
                break
            except Exception as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                retryable, status_code = _is_retryable_error(e)
                error_code = str(status_code) if status_code else "error"
                log_usage(
                    token=key,
                    model=model,
                    call_source="anthropic_client",
                    error_code=error_code,
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                if status_code == 429:
                    _429_count += 1
                    if _429_count <= _CLAUDE_429_MAX_RETRIES:
                        wait = _retry_delay(_429_count - 1, 429)
                        logger.warning(
                            "claude_429_retry: key=%s model=%s attempt=%d/%d wait=%.1fs",
                            key[:12], model, _429_count, _CLAUDE_429_MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    mark_token_rate_limited(key, _get_error_headers(e))
                    logger.warning(
                        "claude_429_limit_exhausted: key=%s model=%s after=%d retries → next token",
                        key[:12], model, _CLAUDE_429_MAX_RETRIES,
                    )
                    break
                if retryable and retry_count < _CLAUDE_MAX_RETRIES:
                    wait = _retry_delay(retry_count, status_code)
                    logger.warning(
                        "claude_bg_retry: key=%s retry_count=%d/%d status=%s wait=%.1fs last_error=%s",
                        key[:12],
                        retry_count + 1,
                        _CLAUDE_MAX_RETRIES,
                        status_code or "timeout",
                        wait,
                        str(e)[:160],
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "claude_bg_error: key=%s model=%s retry_count=%d status=%s last_error=%s",
                    key[:12],
                    model,
                    retry_count,
                    status_code or "n/a",
                    str(e)[:160],
                )
                break
        if last_error is not None:
            logger.warning(
                "claude_bg_token_failed: key=%s model=%s max_retries=%d last_error=%s",
                key[:12],
                model,
                _CLAUDE_MAX_RETRIES,
                str(last_error)[:160],
            )

    # 3순위: Gemini 2.5 Flash (LiteLLM 경유)
    _lc = get_litellm_config()
    if _GEMINI_FALLBACK_ENABLED and _lc["key"]:
        try:
            return await _call_litellm(prompt, _GEMINI_FALLBACK_MODEL, max_tokens, system)
        except Exception as e:
            logger.warning("gemini_bg_fallback_error: %s", str(e)[:80])

    # 4순위: qwen3-235b (DashScope)
    if _DASHSCOPE_API_KEY:
        try:
            return await _call_dashscope(prompt, "qwen3-235b", max_tokens, system)
        except Exception as e:
            logger.warning("qwen3_235b_fallback_error: %s", str(e)[:80])

    # 5순위(최종): LiteLLM 저비용 체인 — Claude OAuth 만료/Gemini 고갈 시 가용성 유지
    if _lc.get("key"):
        for _fb_model in _BG_FALLBACK_MODELS:
            try:
                _text = await _call_litellm(prompt, _fb_model, max_tokens, system)
                if _text:
                    logger.info("bg_llm_last_resort_ok: model=%s", _fb_model)
                    return _text
            except Exception as e:
                logger.warning(
                    "bg_llm_last_resort_error: model=%s error=%s", _fb_model, str(e)[:80]
                )

    logger.error("all_bg_llm_failed: claude+gemini+qwen3+litellm_chain exhausted")
    return None


async def call_background_llm(
    prompt: str,
    system: str = "",
    max_tokens: int = 1000,
    tenant_id: Optional[str] = None,
) -> str:
    """배경 서비스용 LLM 호출 — qwen-turbo(DashScope) 1순위, claude-haiku 폴백.

    compaction, memory_manager, fact_extractor, experience_learner,
    quality_feedback_loop, self_evaluator, smart_search, code_reviewer 등
    OAuth 한도를 소비하지 않는 배경 작업에서 사용.
    """
    global _bg_qwen_fail_streak
    if tenant_id:
        from app.services.tenant_usage_limits import check_tenant_usage_limit

        await check_tenant_usage_limit(tenant_id, operation="background_llm", projected_calls=1)
    t0 = time.time()

    # 1순위: qwen-turbo (DashScope 직접)
    try:
        result = await _call_dashscope(prompt, "qwen-turbo", max_tokens, system or None)
        if result:
            _bg_qwen_fail_streak = 0
            await _bg_llm_log(
                "background", "qwen-turbo", True,
                latency_ms=int((time.time() - t0) * 1000),
                tenant_id=tenant_id,
            )
            return result
    except Exception as e:
        logger.warning("call_background_llm_qwen_failed: %s", str(e)[:80])
        _bg_qwen_fail_streak += 1
        await _bg_llm_log("background", "qwen-turbo", False, error_code="qwen_failed", tenant_id=tenant_id)
        if _bg_qwen_fail_streak >= 3:  # qwen-turbo 조기 감지를 위해 3회로 낮춤 (AADS-204)
            await _notify_bg_llm_alert(_bg_qwen_fail_streak)

    # 2순위: claude-haiku (OAuth 폴백)
    fallback = await call_llm_with_fallback(
        prompt=prompt,
        system=system or None,
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        tenant_id=tenant_id,
    )
    return fallback or ""


async def _bg_llm_log(
    service_name: str,
    model: str,
    success: bool,
    latency_ms: int = 0,
    error_code: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tenant_id: Optional[str] = None,
) -> None:
    """bg_llm_usage_log 테이블에 호출 결과 INSERT. DB 실패 시 예외 무시."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bg_llm_usage_log
                  (service_name, model, success, input_tokens, output_tokens, latency_ms, error_code, tenant_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8::uuid, public.aads_internal_tenant_id()))
                """,
                service_name, model, success,
                input_tokens, output_tokens, latency_ms, error_code, tenant_id,
            )
    except Exception as e:
        logger.debug("bg_llm_log_failed: %s", str(e)[:80])


async def _notify_bg_llm_alert(streak: int) -> None:
    """qwen-turbo 연속 실패 시 텔레그램 긴급알림."""
    try:
        from app.services.telegram_bot import get_telegram_bot
        bot = get_telegram_bot()
        if bot and bot.is_ready:
            await bot.send_message(
                f"\U0001f6a8 *qwen-turbo 연속 실패 ({streak}회)*\n"
                f"Background LLM이 {streak}회 연속 실패했습니다.\n"
                f"claude-haiku 폴백 중. DashScope API 상태 확인 필요. (AADS-204)"
            )
    except Exception as e:
        logger.debug("bg_llm_alert_failed: %s", str(e)[:80])


async def call_llm_messages_with_fallback(**kwargs) -> object:
    """Anthropic Messages API 직접 호출 + 2계정 폴백 (서브에이전트/tool-use용).

    비Claude 모델(qwen-turbo 등) 지정 시 DashScope/LiteLLM으로 직접 라우팅,
    Anthropic Message 호환 객체로 래핑하여 반환.

    Args:
        **kwargs: AsyncAnthropic.messages.create()에 전달할 전체 파라미터

    Returns:
        Anthropic Message 응답 객체 또는 _LiteLLMResponse (비Claude 경유 시)

    Raises:
        Exception: 모든 키에서 실패 시 마지막 예외를 raise
    """
    tenant_id = kwargs.pop("tenant_id", None)
    _model = kwargs.get("model", "unknown")
    if tenant_id:
        from app.services.tenant_usage_limits import check_tenant_usage_limit

        await check_tenant_usage_limit(tenant_id, operation=f"llm_messages:{_model}", projected_calls=1)

    # 비Claude 모델 → DashScope/LiteLLM 직접
    if not _model.startswith("claude"):
        if _model.startswith("qwen"):
            return await _call_dashscope_messages(
                model=_model,
                messages=kwargs.get("messages", []),
                max_tokens=kwargs.get("max_tokens", 256),
                system=kwargs.get("system"),
            )
        return await _call_litellm_messages(
            model=_model,
            messages=kwargs.get("messages", []),
            max_tokens=kwargs.get("max_tokens", 256),
            system=kwargs.get("system"),
        )

    from app.services.oauth_usage_tracker import log_usage

    keys_to_try = get_available_tokens()
    last_error: Optional[Exception] = None

    for key in keys_to_try:
        _429_count = 0
        for retry_count in range(_CLAUDE_MAX_RETRIES + 1):
            t0 = time.monotonic()
            try:
                client = create_anthropic_client(token=key)
                raw = await client.messages.with_raw_response.create(**kwargs)
                resp = raw.parse()
                duration_ms = int((time.monotonic() - t0) * 1000)
                log_usage(
                    token=key,
                    model=_model,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                    headers=raw.headers,
                    call_source="anthropic_client_msg",
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                return resp
            except (httpx.ReadTimeout, anthropic.APITimeoutError) as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                log_usage(
                    token=key,
                    model=_model,
                    call_source="anthropic_client_msg",
                    error_code="timeout",
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                if retry_count < _CLAUDE_MAX_RETRIES:
                    wait = _retry_delay(retry_count, status_code=None)
                    logger.warning(
                        "claude_msg_retry_timeout: key=%s retry_count=%d/%d wait=%.1fs last_error=%s",
                        key[:12],
                        retry_count + 1,
                        _CLAUDE_MAX_RETRIES,
                        wait,
                        str(e)[:160],
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "claude_msg_timeout_exhausted: key=%s model=%s retry_count=%d last_error=%s",
                    key[:12],
                    _model,
                    retry_count,
                    str(e)[:160],
                )
                break
            except Exception as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                retryable, status_code = _is_retryable_error(e)
                error_code = str(status_code) if status_code else "error"
                log_usage(
                    token=key,
                    model=_model,
                    call_source="anthropic_client_msg",
                    error_code=error_code,
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                if status_code == 429:
                    _429_count += 1
                    if _429_count <= _CLAUDE_429_MAX_RETRIES:
                        wait = _retry_delay(_429_count - 1, 429)
                        logger.warning(
                            "claude_msg_429_retry: key=%s attempt=%d/%d wait=%.1fs",
                            key[:12], _429_count, _CLAUDE_429_MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    mark_token_rate_limited(key, _get_error_headers(e))
                    logger.warning(
                        "claude_msg_429_exhausted: key=%s after=%d retries → next token",
                        key[:12], _CLAUDE_429_MAX_RETRIES,
                    )
                    break
                if retryable and retry_count < _CLAUDE_MAX_RETRIES:
                    wait = _retry_delay(retry_count, status_code)
                    logger.warning(
                        "claude_msg_retry: key=%s retry_count=%d/%d status=%s wait=%.1fs last_error=%s",
                        key[:12],
                        retry_count + 1,
                        _CLAUDE_MAX_RETRIES,
                        status_code or "timeout",
                        wait,
                        str(e)[:160],
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "claude_msg_error: key=%s model=%s retry_count=%d status=%s last_error=%s",
                    key[:12],
                    _model,
                    retry_count,
                    status_code or "n/a",
                    str(e)[:160],
                )
                break

    raise last_error or RuntimeError("no API keys configured")


# ── DashScope 직접 호출 (Alibaba Qwen 모델) ─────────────────────────

_FALLBACK_QUICK_RETRIES = 3
_FALLBACK_QUICK_DELAYS = (1.0, 2.0, 4.0)
_FALLBACK_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}


def _is_fallback_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and status in _FALLBACK_RETRYABLE_STATUS:
        return True
    return isinstance(exc, (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException))


async def _call_dashscope(
    prompt: str,
    model: str,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> str:
    """DashScope API 직접 호출 (OpenAI 호환). 일시 오류 시 3회 빠른 재시도."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max(max_tokens, 512),
    }

    last_err: Optional[Exception] = None
    for attempt in range(_FALLBACK_QUICK_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{_DASHSCOPE_BASE_URL}/chat/completions",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {_DASHSCOPE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content") or ""
                if not content:
                    raise ValueError(f"DashScope returned empty content for model {model}")
                logger.info("dashscope_bg_ok: model=%s tokens=%s", model, data.get("usage", {}))
                return content
        except Exception as e:
            last_err = e
            if attempt < _FALLBACK_QUICK_RETRIES and _is_fallback_retryable(e):
                wait = _FALLBACK_QUICK_DELAYS[attempt]
                logger.warning("dashscope_quick_retry: model=%s attempt=%d wait=%.1fs err=%s", model, attempt + 1, wait, str(e)[:80])
                await asyncio.sleep(wait)
                continue
            raise
    raise last_err  # unreachable but satisfies type checker


async def _call_dashscope_messages(
    model: str,
    messages: list,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> _LiteLLMResponse:
    """DashScope API 직접 Messages 호출 — Anthropic Response 호환 래핑."""
    oai_msgs = []
    if system:
        oai_msgs.append({"role": "system", "content": system})
    for m in messages:
        oai_msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    body = {
        "model": model,
        "messages": oai_msgs,
        "max_tokens": max(max_tokens, 512),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_DASHSCOPE_BASE_URL}/chat/completions",
            json=body,
            headers={
                "Authorization": f"Bearer {_DASHSCOPE_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        if not content:
            raise ValueError(f"DashScope returned empty content for model {model}")
        usage_data = data.get("usage", {})
        logger.info("dashscope_msg_ok: model=%s tokens=%s", model, usage_data)
        return _LiteLLMResponse(content, model, usage_data)


# ── LiteLLM 프록시 호출 (Gemini 등) ─────────────────────────────────

async def _call_litellm(
    prompt: str,
    model: str,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> str:
    """LiteLLM 프록시 경유 텍스트 생성 (OpenAI 호환 API). 일시 오류 시 3회 빠른 재시도."""
    _lc = get_litellm_config()
    url = f"{_lc['url']}/v1/chat/completions"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max(max_tokens, 512),
    }

    last_err: Optional[Exception] = None
    for attempt in range(_FALLBACK_QUICK_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {_lc['key']}"},
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content") or ""
                if not content:
                    raise ValueError(f"LiteLLM returned empty content for model {model}")
                return content
        except Exception as e:
            last_err = e
            if attempt < _FALLBACK_QUICK_RETRIES and _is_fallback_retryable(e):
                wait = _FALLBACK_QUICK_DELAYS[attempt]
                logger.warning("litellm_quick_retry: model=%s attempt=%d wait=%.1fs err=%s", model, attempt + 1, wait, str(e)[:80])
                await asyncio.sleep(wait)
                continue
            raise
    raise last_err


async def _call_litellm_messages(
    model: str,
    messages: list,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> _LiteLLMResponse:
    """LiteLLM 프록시 경유 Messages 호출 — Anthropic Response 호환 래핑."""
    _lc = get_litellm_config()
    url = f"{_lc['url']}/v1/chat/completions"

    oai_msgs = []
    if system:
        oai_msgs.append({"role": "system", "content": system})
    for m in messages:
        oai_msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    body = {
        "model": model,
        "messages": oai_msgs,
        "max_tokens": max(max_tokens, 512),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {_lc['key']}"},
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        if not content:
            raise ValueError(f"LiteLLM returned empty content for model {model}")
        usage_data = data.get("usage", {})
        return _LiteLLMResponse(content, model, usage_data)
