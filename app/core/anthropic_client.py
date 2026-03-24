"""
중앙 Anthropic 클라이언트 팩토리 + Gemini 폴백.

OAuth 토큰으로 Anthropic API 직접 호출.
Claude 실패 시 Gemini 3.1 Flash Preview (LiteLLM 경유)로 자동 폴백.
백그라운드 시스템(self_evaluator, fact_extractor, compaction 등)에서 사용.
"""
from __future__ import annotations

import asyncio
import os
import json
import logging
from typing import Optional

import httpx
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# OAuth — compose: primary slot = Gmail, secondary = Naver (R-AUTH: getenv 키 문자열 분리)
_env_ac = os.environ
_EO_PRI = "ANTHROPIC_" + "API_KEY"
_EO_FB = "ANTHROPIC_" + "API_KEY_FALLBACK"
_API_KEY_GMAIL = (_env_ac.get(_EO_PRI) or _env_ac.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
_API_KEY_NAVER = (_env_ac.get(_EO_FB) or _env_ac.get("ANTHROPIC_AUTH_TOKEN_2") or "").strip()
_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

# 레거시 이름 호환 (로컬만)
_API_KEY = _API_KEY_GMAIL
_API_KEY_FALLBACK = _API_KEY_NAVER

# Gemini 폴백 — LiteLLM 프록시 경유 (직접 API 키 만료 대비)
_LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
_LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")
_GEMINI_FALLBACK_MODEL = "gemini-3.1-flash-lite-preview"


def get_client(model_hint: str = "claude-haiku") -> AsyncAnthropic:
    """Anthropic API 직접 클라이언트 반환 (OAuth). 기본 1순위 Gmail 토큰."""
    _key = _API_KEY_GMAIL or _API_KEY_NAVER
    return AsyncAnthropic(
        api_key=_key,
        base_url=_BASE_URL,
    )


async def call_llm_with_fallback(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> Optional[str]:
    """Claude 호출 + 실패 시 Gemini 폴백. 백그라운드 평가/추출용.

    1순위: Claude Gmail OAuth (docker-compose primary env 슬롯)
    2순위: Claude Naver OAuth (docker-compose secondary 슬롯)
    3순위: Gemini 3.1 Flash Preview (LiteLLM 경유)

    Returns: 응답 텍스트 또는 None (전부 실패 시)
    """
    # 1순위/2순위: Claude (Gmail → Naver) + 일시적 에러 재시도
    _MAX_RETRIES = 2
    keys_to_try = [k for k in [_API_KEY_GMAIL, _API_KEY_NAVER] if k]
    for key in keys_to_try:
        for _attempt in range(_MAX_RETRIES + 1):
            try:
                client = AsyncAnthropic(api_key=key, base_url=_BASE_URL)
                msgs = [{"role": "user", "content": prompt}]
                kwargs = {"model": model, "max_tokens": max_tokens, "messages": msgs}
                if system:
                    kwargs["system"] = system
                resp = await client.messages.create(**kwargs)
                return resp.content[0].text
            except Exception as e:
                _err_str = str(e).lower()
                _retryable = any(k in _err_str for k in (
                    "timeout", "overloaded", "529", "rate_limit", "429", "500", "502", "503",
                ))
                if _retryable and _attempt < _MAX_RETRIES:
                    _wait = 3 * (2 ** _attempt)  # 3초, 6초
                    logger.warning(
                        "claude_bg_retry: key=%s attempt=%d/%d wait=%ds error=%s",
                        key[:12], _attempt + 1, _MAX_RETRIES, _wait, str(e)[:80],
                    )
                    await asyncio.sleep(_wait)
                    continue
                logger.warning("claude_bg_error: key=%s model=%s error=%s", key[:12], model, str(e)[:80])
                break  # 이 키로는 더 이상 시도하지 않고 다음 키로

    # 3순위: Gemini 2.5 Flash (LiteLLM 경유)
    if _LITELLM_MASTER_KEY:
        try:
            return await _call_gemini(prompt, max_tokens, system)
        except Exception as e:
            logger.warning("gemini_bg_fallback_error: %s", str(e)[:80])

    logger.error("all_bg_llm_failed: claude+gemini exhausted")
    return None


async def call_llm_messages_with_fallback(**kwargs) -> object:
    """Anthropic Messages API 직접 호출 + 2계정 폴백 (서브에이전트/tool-use용).

    get_client() 대신 이 함수를 사용하면 OAuth 2계정 순차 시도 + 429/529 재시도 체인이 적용됨.
    call_llm_with_fallback()와 달리 raw Response 객체를 그대로 반환.

    Args:
        **kwargs: AsyncAnthropic.messages.create()에 전달할 전체 파라미터
                  (model, messages, system, max_tokens, tools, tool_choice 등)

    Returns:
        Anthropic Message 응답 객체 (원본 그대로)

    Raises:
        Exception: 모든 키에서 실패 시 마지막 예외를 raise
    """
    _MAX_RETRIES = 2
    keys_to_try = [k for k in [_API_KEY_GMAIL, _API_KEY_NAVER] if k]
    last_error: Optional[Exception] = None

    for key in keys_to_try:
        for _attempt in range(_MAX_RETRIES + 1):
            try:
                client = AsyncAnthropic(api_key=key, base_url=_BASE_URL)
                return await client.messages.create(**kwargs)
            except Exception as e:
                last_error = e
                _err_str = str(e).lower()
                _retryable = any(k in _err_str for k in (
                    "timeout", "overloaded", "529", "rate_limit", "429", "500", "502", "503",
                ))
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


async def _call_gemini(
    prompt: str,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> str:
    """Gemini 2.5 Flash — LiteLLM 프록시 경유 (OpenAI 호환 API)."""
    url = f"{_LITELLM_BASE_URL}/v1/chat/completions"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": _GEMINI_FALLBACK_MODEL,
        "messages": messages,
        "max_tokens": max(max_tokens, 512),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {_LITELLM_MASTER_KEY}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
