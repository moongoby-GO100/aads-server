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
import json
import logging
import time
from typing import Optional

import httpx
from anthropic import AsyncAnthropic

from app.core.auth_provider import (
    get_oauth_tokens, get_base_url, get_litellm_config, create_anthropic_client,
)

logger = logging.getLogger(__name__)

_GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
_DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_DASHSCOPE_API_KEY = os.getenv("ALIBABA_API_KEY", "")

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


async def call_llm_with_fallback(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> Optional[str]:
    """Claude 호출 + 실패 시 Gemini 폴백. 백그라운드 평가/추출용.

    비Claude 모델(qwen-turbo 등) 지정 시 DashScope/LiteLLM으로 직접 라우팅.

    1순위: Claude Naver 토큰
    2순위: Claude Gmail 토큰
    3순위: Gemini 2.5 Flash (LiteLLM 경유)

    Returns: 응답 텍스트 또는 None (전부 실패 시)
    """
    # 비Claude 모델 → DashScope/LiteLLM 직접
    if not model.startswith("claude"):
        try:
            if model.startswith("qwen"):
                return await _call_dashscope(prompt, model, max_tokens, system)
            return await _call_litellm(prompt, model, max_tokens, system)
        except Exception as e:
            logger.warning("litellm_bg_error: model=%s error=%s", model, str(e)[:80])
            # 실패 시 Gemini 폴백
            try:
                return await _call_litellm(prompt, _GEMINI_FALLBACK_MODEL, max_tokens, system)
            except Exception as e2:
                logger.warning("litellm_bg_gemini_fallback_error: %s", str(e2)[:80])
            return None

    from app.services.oauth_usage_tracker import log_usage

    _MAX_RETRIES = 2
    keys_to_try = get_oauth_tokens()
    for key in keys_to_try:
        for _attempt in range(_MAX_RETRIES + 1):
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
                )
                return resp.content[0].text
            except Exception as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                _err_str = str(e).lower()
                _retryable = any(k in _err_str for k in (
                    "timeout", "overloaded", "529", "rate_limit", "429", "500", "502", "503",
                ))
                _err_code = None
                for code in ("429", "402", "401", "403", "500", "502", "503", "529"):
                    if code in _err_str:
                        _err_code = code
                        break
                log_usage(
                    token=key, model=model,
                    call_source="anthropic_client",
                    error_code=_err_code or "error",
                    duration_ms=duration_ms,
                )
                if _retryable and _attempt < _MAX_RETRIES:
                    _wait = 3 * (2 ** _attempt)
                    logger.warning(
                        "claude_bg_retry: key=%s attempt=%d/%d wait=%ds error=%s",
                        key[:12], _attempt + 1, _MAX_RETRIES, _wait, str(e)[:80],
                    )
                    await asyncio.sleep(_wait)
                    continue
                logger.warning("claude_bg_error: key=%s model=%s error=%s", key[:12], model, str(e)[:80])
                break

    # 3순위: Gemini 2.5 Flash (LiteLLM 경유)
    _lc = get_litellm_config()
    if _lc["key"]:
        try:
            return await _call_litellm(prompt, _GEMINI_FALLBACK_MODEL, max_tokens, system)
        except Exception as e:
            logger.warning("gemini_bg_fallback_error: %s", str(e)[:80])

    logger.error("all_bg_llm_failed: claude+gemini exhausted")
    return None


async def call_background_llm(
    prompt: str,
    system: str = "",
    max_tokens: int = 1000,
) -> str:
    """배경 서비스용 LLM 호출 — qwen-turbo(DashScope) 1순위, claude-haiku 폴백.

    compaction, memory_manager, fact_extractor, experience_learner,
    quality_feedback_loop, self_evaluator, smart_search, code_reviewer 등
    OAuth 한도를 소비하지 않는 배경 작업에서 사용.
    """
    global _bg_qwen_fail_streak
    t0 = time.time()

    # 1순위: qwen-turbo (DashScope 직접)
    try:
        result = await _call_dashscope(prompt, "qwen-turbo", max_tokens, system or None)
        if result:
            _bg_qwen_fail_streak = 0
            await _bg_llm_log(
                "background", "qwen-turbo", True,
                latency_ms=int((time.time() - t0) * 1000),
            )
            return result
    except Exception as e:
        logger.warning("call_background_llm_qwen_failed: %s", str(e)[:80])
        _bg_qwen_fail_streak += 1
        await _bg_llm_log("background", "qwen-turbo", False, error_code="qwen_failed")
        if _bg_qwen_fail_streak >= 3:  # qwen-turbo 조기 감지를 위해 3회로 낮춤 (AADS-204)
            await _notify_bg_llm_alert(_bg_qwen_fail_streak)

    # 2순위: claude-haiku (OAuth 폴백)
    fallback = await call_llm_with_fallback(
        prompt=prompt,
        system=system or None,
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
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
) -> None:
    """bg_llm_usage_log 테이블에 호출 결과 INSERT. DB 실패 시 예외 무시."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bg_llm_usage_log
                  (service_name, model, success, input_tokens, output_tokens, latency_ms, error_code)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                service_name, model, success,
                input_tokens, output_tokens, latency_ms, error_code,
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
    _model = kwargs.get("model", "unknown")

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

    _MAX_RETRIES = 2
    keys_to_try = get_oauth_tokens()
    last_error: Optional[Exception] = None

    for key in keys_to_try:
        for _attempt in range(_MAX_RETRIES + 1):
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
                )
                return resp
            except Exception as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                _err_str = str(e).lower()
                _retryable = any(k in _err_str for k in (
                    "timeout", "overloaded", "529", "rate_limit", "429", "500", "502", "503",
                ))
                _err_code = None
                for code in ("429", "402", "401", "403", "500", "502", "503", "529"):
                    if code in _err_str:
                        _err_code = code
                        break
                log_usage(
                    token=key, model=_model,
                    call_source="anthropic_client_msg",
                    error_code=_err_code or "error",
                    duration_ms=duration_ms,
                )
                if _retryable and _attempt < _MAX_RETRIES:
                    _wait = 3 * (2 ** _attempt)
                    logger.warning(
                        "claude_msg_retry: key=%s attempt=%d/%d wait=%ds error=%s",
                        key[:12], _attempt + 1, _MAX_RETRIES, _wait, str(e)[:80],
                    )
                    await asyncio.sleep(_wait)
                    continue
                logger.warning("claude_msg_error: key=%s error=%s", key[:12], str(e)[:80])
                break

    raise last_error or RuntimeError("no API keys configured")


# ── DashScope 직접 호출 (Alibaba Qwen 모델) ─────────────────────────

async def _call_dashscope(
    prompt: str,
    model: str,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> str:
    """DashScope API 직접 호출 (OpenAI 호환)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "messages": messages,
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
        logger.info("dashscope_bg_ok: model=%s tokens=%s", model, data.get("usage", {}))
        return content


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
    """LiteLLM 프록시 경유 텍스트 생성 (OpenAI 호환 API)."""
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
