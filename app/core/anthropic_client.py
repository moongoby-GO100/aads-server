"""
중앙 Anthropic 클라이언트 팩토리 + Gemini 폴백.

OAuth 토큰으로 Anthropic API 직접 호출.
Claude 실패 시 Gemini 3.1 Flash Preview (LiteLLM 경유)로 자동 폴백.
백그라운드 시스템(self_evaluator, fact_extractor, compaction 등)에서 사용.
"""
import os
import json
import logging
from typing import Optional

import httpx
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# OAuth 토큰 직접 사용 (Agent SDK 채팅 AI와 동일 경로)
_API_KEY = os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("ANTHROPIC_AUTH_TOKEN", "")
_API_KEY_FALLBACK = os.getenv("ANTHROPIC_API_KEY_FALLBACK", "")
_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

# Gemini 폴백 (직접 API 호출)
_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# LiteLLM의 Gemini 키 폴백
if not _GEMINI_API_KEY:
    _GEMINI_API_KEY = os.getenv("LITELLM_GEMINI_KEY", "")
_GEMINI_FALLBACK_MODEL = "gemini-3.1-flash-lite-preview"


def get_client(model_hint: str = "claude-haiku") -> AsyncAnthropic:
    """Anthropic API 직접 클라이언트 반환 (OAuth 토큰 인증)."""
    return AsyncAnthropic(
        api_key=_API_KEY,
        base_url=_BASE_URL,
    )


async def call_llm_with_fallback(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> Optional[str]:
    """Claude 호출 + 실패 시 Gemini 폴백. 백그라운드 평가/추출용.

    1순위: Claude Naver 토큰
    2순위: Claude Gmail 토큰
    3순위: Gemini 3.1 Flash Preview (LiteLLM 경유)

    Returns: 응답 텍스트 또는 None (전부 실패 시)
    """
    # 1순위/2순위: Claude (Naver → Gmail)
    keys_to_try = [k for k in [_API_KEY, _API_KEY_FALLBACK] if k]
    for key in keys_to_try:
        try:
            client = AsyncAnthropic(api_key=key, base_url=_BASE_URL)
            msgs = [{"role": "user", "content": prompt}]
            kwargs = {"model": model, "max_tokens": max_tokens, "messages": msgs}
            if system:
                kwargs["system"] = system
            resp = await client.messages.create(**kwargs)
            return resp.content[0].text
        except Exception as e:
            logger.warning("claude_bg_error: key=%s model=%s error=%s", key[:12], model, str(e)[:80])
            continue

    # 3순위: Gemini 2.5 Flash (직접 API)
    if _GEMINI_API_KEY:
        try:
            return await _call_gemini(prompt, max_tokens, system)
        except Exception as e:
            logger.warning("gemini_bg_fallback_error: %s", str(e)[:80])

    logger.error("all_bg_llm_failed: claude+gemini exhausted")
    return None


async def _call_gemini(
    prompt: str,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> str:
    """Gemini 2.5 Flash — 직접 REST API 호출."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_FALLBACK_MODEL}:generateContent"

    contents = []
    if system:
        contents.append({"role": "user", "parts": [{"text": f"[SYSTEM] {system}"}]})
        contents.append({"role": "model", "parts": [{"text": "Understood."}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    body = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max(max_tokens, 512)},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, params={"key": _GEMINI_API_KEY}, json=body)
        resp.raise_for_status()
        data = resp.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        for p in parts:
            if p.get("text"):
                return p["text"]
        raise ValueError("Gemini: no text in response")
