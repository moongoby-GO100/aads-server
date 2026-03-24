"""
벤치마크 사양 추출기 — T-027.

BenchmarkSpecExtractor:
  - extract_spec(benchmark_frames) → dict (제작 사양 JSON)
  - spec_to_ffmpeg_params(spec) → dict (FFmpeg 파라미터)
  - save_spec(project_id, channel_name, spec) → None (system_memory 저장)

LLM: Gemini 2.5 Flash Vision (primary) → Claude Sonnet Vision (fallback)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# 사양 추출 프롬프트
# ---------------------------------------------------------------------------

SPEC_PROMPT = """
이 영상 프레임들을 분석하여 제작 사양을 추출하세요.

## 추출 항목 (JSON)
{
  "subtitle": {
    "font_size_estimate": "px",
    "position": "bottom_percent",
    "background_box": true/false,
    "background_opacity": 0.0-1.0,
    "font_color": "#hex",
    "max_chars_per_line": 0
  },
  "background": {
    "resolution": "WxH",
    "style": "stock_video|ai_generated|solid|gradient",
    "dominant_colors": ["#hex"],
    "brightness": "dark|medium|bright",
    "keywords": ["keyword1", "keyword2"]
  },
  "composition": {
    "safe_zone_top_percent": 0,
    "safe_zone_bottom_percent": 0,
    "text_area_percent": 0
  },
  "branding": {
    "color_palette": ["#hex"],
    "tone": "professional|casual|educational|dramatic"
  }
}

반드시 위 형식의 JSON만 반환하세요. 추가 설명 없이 JSON만.
"""


# ---------------------------------------------------------------------------
# 헬퍼 함수
# ---------------------------------------------------------------------------

def _image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _extract_json(text: str) -> dict:
    """LLM 응답에서 JSON 블록 추출."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON 없음: {text[:200]}")


def _default_spec() -> dict:
    """기본 사양 (LLM 실패 시 사용)."""
    return {
        "subtitle": {
            "font_size_estimate": "48px",
            "position": "10%",
            "background_box": True,
            "background_opacity": 0.8,
            "font_color": "#ffffff",
            "max_chars_per_line": 20,
        },
        "background": {
            "resolution": "1080x1920",
            "style": "stock_video",
            "dominant_colors": ["#000000"],
            "brightness": "medium",
            "keywords": [],
        },
        "composition": {
            "safe_zone_top_percent": 10,
            "safe_zone_bottom_percent": 10,
            "text_area_percent": 20,
        },
        "branding": {
            "color_palette": ["#ffffff", "#000000"],
            "tone": "professional",
        },
    }


# ---------------------------------------------------------------------------
# LLM 호출
# ---------------------------------------------------------------------------

async def _call_gemini_vision_frames(frames_b64: List[str], prompt: str) -> str:
    """Gemini 2.5 Flash Vision으로 여러 프레임 분석."""
    import google.generativeai as genai
    import PIL.Image
    import io

    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not set")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    contents = [prompt]
    for b64 in frames_b64[:5]:  # 최대 5프레임
        image_bytes = base64.b64decode(b64)
        pil_image = PIL.Image.open(io.BytesIO(image_bytes))
        contents.append(pil_image)

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: model.generate_content(contents),
    )
    return response.text


async def _call_claude_vision_frames(frames_b64: List[str], prompt: str) -> str:
    """Claude Sonnet Vision 폴백 (첫 번째 프레임만)."""
    import anthropic

    from app.core.auth_provider import has_valid_token
    if not has_valid_token():
        raise ValueError("No valid auth token (R-AUTH)")

    from app.core.anthropic_client import get_client as _get_bs_client
    client = _get_bs_client()

    content_blocks: List[dict] = []
    for b64 in frames_b64[:3]:  # 최대 3프레임
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        })
    content_blocks.append({"type": "text", "text": prompt})

    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": content_blocks}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# BenchmarkSpecExtractor 클래스
# ---------------------------------------------------------------------------

class BenchmarkSpecExtractor:
    """벤치마크 영상 프레임에서 제작 사양 추출."""

    SPEC_PROMPT = SPEC_PROMPT

    async def extract_spec(self, benchmark_frames: List[str]) -> dict:
        """
        벤치마크 프레임에서 제작 사양 추출 → JSON.

        Args:
            benchmark_frames: base64 인코딩된 프레임 이미지 목록

        Returns:
            사양 dict (subtitle, background, composition, branding)
        """
        if not benchmark_frames:
            logger.warning("extract_spec_no_frames")
            raise ValueError("benchmark_frames가 비어 있습니다. 최소 1개 이상의 프레임이 필요합니다.")

        logger.info("extract_spec_start", frame_count=len(benchmark_frames))

        raw_text = ""
        provider_used = ""

        # Primary: Gemini Vision
        try:
            raw_text = await _call_gemini_vision_frames(benchmark_frames, self.SPEC_PROMPT)
            provider_used = "gemini-1.5-flash"
            logger.info("gemini_vision_spec_success")
        except Exception as e:
            logger.warning("gemini_vision_spec_failed_fallback", error=str(e))
            # Fallback: Claude Sonnet Vision
            try:
                raw_text = await _call_claude_vision_frames(benchmark_frames, self.SPEC_PROMPT)
                provider_used = "claude-sonnet-4-5"
                logger.info("claude_vision_spec_fallback_success")
            except Exception as e2:
                logger.error("all_vision_providers_failed_spec", error=str(e2))
                logger.warning("using_default_spec")
                return _default_spec()

        # JSON 파싱
        try:
            spec = _extract_json(raw_text)
            logger.info("extract_spec_done", provider=provider_used)
            return spec
        except Exception as e:
            logger.error("spec_json_parse_error", raw=raw_text[:300], error=str(e))
            logger.warning("using_default_spec_parse_error")
            return _default_spec()

    async def spec_to_ffmpeg_params(self, spec: dict) -> dict:
        """
        사양 → FFmpeg 파라미터 매핑.

        예: font_size → fontsize=48, background_opacity → alpha=0.8
        반환: {"fontsize":48, "box_alpha":0.8, "margin_bottom":"20%", ...}
        """
        subtitle = spec.get("subtitle", {})
        background = spec.get("background", {})
        composition = spec.get("composition", {})
        branding = spec.get("branding", {})

        # font_size 추출 (e.g. "48px" → 48)
        font_size_raw = subtitle.get("font_size_estimate", "48px")
        try:
            fontsize = int(str(font_size_raw).replace("px", "").strip())
        except (ValueError, TypeError):
            fontsize = 48

        # background_opacity → box_alpha
        box_alpha = float(subtitle.get("background_opacity", 0.8))

        # position → margin_bottom
        position_raw = subtitle.get("position", "10%")
        try:
            if isinstance(position_raw, str) and "%" in position_raw:
                margin_bottom = position_raw
            else:
                margin_bottom = f"{int(float(str(position_raw).replace('%', '').strip()))}%"
        except (ValueError, TypeError):
            margin_bottom = "10%"

        # max_chars_per_line → line_length
        max_chars = int(subtitle.get("max_chars_per_line", 20))

        # font_color → fontcolor
        fontcolor = subtitle.get("font_color", "#ffffff")

        # resolution → scale
        resolution = background.get("resolution", "1080x1920")

        # safe_zone
        safe_top = int(composition.get("safe_zone_top_percent", 10))
        safe_bottom = int(composition.get("safe_zone_bottom_percent", 10))

        # background_box
        box_enabled = bool(subtitle.get("background_box", True))

        params = {
            "fontsize": fontsize,
            "box_alpha": box_alpha,
            "margin_bottom": margin_bottom,
            "line_length": max_chars,
            "fontcolor": fontcolor,
            "resolution": resolution,
            "safe_zone_top": safe_top,
            "safe_zone_bottom": safe_bottom,
            "box_enabled": box_enabled,
            "tone": branding.get("tone", "professional"),
            "background_style": background.get("style", "stock_video"),
            "brightness": background.get("brightness", "medium"),
        }

        logger.info("spec_to_ffmpeg_params_done", fontsize=fontsize, box_alpha=box_alpha)
        return params

    async def save_spec(self, project_id: str, channel_name: str, spec: dict) -> None:
        """
        system_memory에 저장.
        category: benchmark_specs, key: {project_id}_{channel}
        """
        key = f"{project_id}_{channel_name}"
        value = {
            "spec": spec,
            "project_id": project_id,
            "channel_name": channel_name,
            "saved_at": datetime.utcnow().isoformat(),
        }

        try:
            from app.memory.store import memory_store
            await memory_store.put_system(
                category="benchmark_specs",
                key=key,
                value=value,
                updated_by="benchmark_spec_extractor",
            )
            logger.info("benchmark_spec_saved", project_id=project_id, channel=channel_name, key=key)
        except Exception as e:
            logger.warning("benchmark_spec_save_failed", error=str(e), key=key)

    async def get_spec(self, project_id: str, channel_name: str) -> Optional[dict]:
        """
        system_memory에서 벤치마크 사양 조회.
        """
        key = f"{project_id}_{channel_name}"
        try:
            from app.memory.store import memory_store
            result = await memory_store.get_system(category="benchmark_specs", key=key)
            if result:
                return result.get("spec")
            return None
        except Exception as e:
            logger.warning("benchmark_spec_get_failed", error=str(e), key=key)
            return None


# 싱글톤 인스턴스
benchmark_spec_extractor = BenchmarkSpecExtractor()
