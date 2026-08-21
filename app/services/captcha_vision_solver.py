"""CAPTCHA auto-solver — vision model로 CAPTCHA 이미지의 숫자/문자를 해독한다.

Gemini Flash(1순위) → Claude Haiku(폴백) 경유, LiteLLM 프록시 사용.
최대 3회 재시도하며, 매 시도마다 새 스크린샷을 촬영한다.
"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_CAPTCHA_PROMPT = (
    "이 이미지는 웹사이트 로그인 페이지 스크린샷입니다. "
    "화면에 CAPTCHA(보안문자/자동입력방지) 숫자 또는 문자가 보입니다. "
    "CAPTCHA 영역에 표시된 숫자/문자를 정확히 읽어서 그것만 출력하세요. "
    "다른 설명이나 따옴표 없이 CAPTCHA 값만 출력하세요. "
    "예: 화면에 보안문자 '38471'이 보이면 38471"
)

_VISION_MODELS = ("gemini-2.5-flash", "claude-haiku-4-5-20251001")


async def _call_vision(image_b64: str) -> str:
    import httpx

    litellm_url = os.getenv("LITELLM_BASE_URL", "http://aads-litellm:4000")
    litellm_key = os.getenv("LITELLM_MASTER_KEY", "")

    for model in _VISION_MODELS:
        try:
            payload = {
                "model": model,
                "max_tokens": 64,
                "temperature": 0,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                            },
                            {"type": "text", "text": _CAPTCHA_PROMPT},
                        ],
                    }
                ],
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{litellm_url}/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {litellm_key}"},
                )
                resp.raise_for_status()
                return str(resp.json()["choices"][0]["message"]["content"] or "")
        except Exception as exc:
            logger.warning("captcha_vision_model_error", model=model, error=str(exc)[:120])
    return ""


async def solve_captcha_with_vision(
    page: Any,
    *,
    screenshot_path: str = "",
    max_retries: int = 3,
) -> str:
    """페이지 스크린샷에서 CAPTCHA 숫자/문자를 비전 모델로 해독한다.

    Returns:
        3~8자리 숫자/문자 문자열, 실패 시 빈 문자열.
    """
    for attempt in range(max_retries):
        try:
            image_bytes: bytes | None = None
            if attempt == 0 and screenshot_path and Path(screenshot_path).is_file():
                image_bytes = Path(screenshot_path).read_bytes()
            else:
                image_bytes = await page.screenshot(full_page=False)
            if not image_bytes:
                continue

            image_b64 = base64.b64encode(image_bytes).decode("ascii")
            raw = await _call_vision(image_b64)
            digits = re.sub(r"[^0-9A-Za-z]", "", raw.strip())
            if 3 <= len(digits) <= 8:
                logger.info("captcha_solved", attempt=attempt + 1, length=len(digits))
                return digits
            logger.info("captcha_no_valid_result", attempt=attempt + 1, raw=raw[:60])
        except Exception as exc:
            logger.warning("captcha_attempt_error", attempt=attempt + 1, error=str(exc)[:120])

        if attempt < max_retries - 1:
            try:
                await page.wait_for_timeout(1500)
            except Exception:
                pass

    logger.warning("captcha_all_attempts_failed", max_retries=max_retries)
    return ""
