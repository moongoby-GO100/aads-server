"""CAPTCHA auto-solver — vision model로 CAPTCHA 이미지의 숫자/문자를 해독한다.

1순위: Anthropic Claude Sonnet (직접 SDK, R-AUTH 준수, 높은 정확도)
2순위: Gemini Flash (REST 직접, GOOGLE_API_KEY)
3순위: LiteLLM 프록시 (복구 시 자동 사용)
최대 3회 재시도하며, 매 시도마다 새 스크린샷을 촬영한다.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_CAPTCHA_PROMPT = (
    "이 스크린샷에는 로그인 페이지의 숫자 CAPTCHA(보안문자)가 있습니다. "
    "CAPTCHA 영역에 표시된 숫자를 정확히 읽으세요. "
    "보통 5~6자리 숫자이며, 배경에 노이즈나 줄이 있을 수 있습니다. "
    "숫자를 하나하나 주의깊게 읽고, 결과 숫자만 출력하세요. "
    "절대 설명이나 따옴표를 붙이지 마세요. 숫자만 출력하세요."
)


async def _call_anthropic_vision(image_b64: str) -> str:
    from app.core.auth_provider import create_anthropic_client

    _msg = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                },
                {"type": "text", "text": _CAPTCHA_PROMPT},
            ],
        }
    ]
    models = ["claude-sonnet-5", "claude-haiku-4-5-20251001"]
    for model_id in models:
        client = create_anthropic_client()
        try:
            kwargs: dict[str, Any] = {"model": model_id, "max_tokens": 32, "messages": _msg}
            if "haiku" in model_id:
                kwargs["temperature"] = 0
            resp = await client.messages.create(**kwargs)
            raw = str(resp.content[0].text) if resp.content else ""
            logger.info("captcha_anthropic_raw", model=model_id, raw=raw[:60])
            return raw
        except Exception as exc:
            logger.warning("captcha_anthropic_fallback", model=model_id, error=str(exc)[:150])
        finally:
            await client.close()
    return ""


async def _call_gemini_vision(image_b64: str) -> str:
    import httpx

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return ""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/png", "data": image_b64}},
                    {"text": _CAPTCHA_PROMPT},
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 32, "temperature": 0},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw = str(data["candidates"][0]["content"]["parts"][0]["text"] or "")
            logger.info("captcha_gemini_raw", raw=raw[:60])
            return raw
    except Exception as exc:
        logger.warning("captcha_gemini_error", error=str(exc)[:200])
        return ""


async def _call_litellm_vision(image_b64: str) -> str:
    import httpx

    litellm_url = os.getenv("LITELLM_BASE_URL", "http://aads-litellm:4000")
    litellm_key = os.getenv("LITELLM_MASTER_KEY", "")
    payload = {
        "model": "gemini-2.5-flash",
        "max_tokens": 32,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": _CAPTCHA_PROMPT},
                ],
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{litellm_url}/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {litellm_key}"},
            )
            resp.raise_for_status()
            raw = str(resp.json()["choices"][0]["message"]["content"] or "")
            logger.info("captcha_litellm_raw", raw=raw[:60])
            return raw
    except Exception as exc:
        logger.warning("captcha_litellm_error", error=str(exc)[:120])
        return ""


def _extract_digits(raw: str) -> str:
    """응답에서 숫자만 추출. 여러 줄이면 가장 긴 숫자열 선택."""
    digits_only = re.sub(r"[^0-9]", "", raw.strip())
    if 4 <= len(digits_only) <= 8:
        return digits_only
    candidates = re.findall(r"\d{4,8}", raw)
    if candidates:
        return max(candidates, key=len)
    return digits_only


async def _call_vision(image_b64: str) -> str:
    for caller, name in [
        (_call_anthropic_vision, "anthropic"),
        (_call_gemini_vision, "gemini"),
        (_call_litellm_vision, "litellm"),
    ]:
        result = await caller(image_b64)
        digits = _extract_digits(result)
        if digits:
            logger.info("captcha_vision_success", provider=name, raw=result.strip()[:30], digits=digits)
            return digits
        if result.strip():
            logger.info("captcha_vision_no_digits", provider=name, raw=result.strip()[:60])
    return ""


async def solve_captcha_with_vision(
    page: Any,
    *,
    screenshot_path: str = "",
    max_retries: int = 3,
) -> str:
    """페이지 스크린샷에서 CAPTCHA 숫자/문자를 비전 모델로 해독한다."""
    for attempt in range(max_retries):
        try:
            image_bytes: bytes | None = None
            if attempt == 0 and screenshot_path and Path(screenshot_path).is_file():
                image_bytes = Path(screenshot_path).read_bytes()
                logger.info("captcha_using_saved_screenshot", path=screenshot_path, size=len(image_bytes))
            else:
                image_bytes = await page.screenshot(full_page=False)
                logger.info("captcha_took_screenshot", attempt=attempt + 1, size=len(image_bytes) if image_bytes else 0)
            if not image_bytes:
                continue

            image_b64 = base64.b64encode(image_bytes).decode("ascii")
            digits = await _call_vision(image_b64)
            if digits:
                logger.info("captcha_solved", attempt=attempt + 1, value=digits)
                return digits
            logger.info("captcha_no_valid_result", attempt=attempt + 1)
        except Exception as exc:
            logger.warning("captcha_attempt_error", attempt=attempt + 1, error=str(exc)[:200])

        if attempt < max_retries - 1:
            try:
                await page.wait_for_timeout(1500)
            except Exception:
                pass

    logger.warning("captcha_all_attempts_failed", max_retries=max_retries)
    return ""
