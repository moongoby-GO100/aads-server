"""
AADS 채팅 이미지 생성 서비스
Google Imagen 4.0 (primary) → GPT-Image-1 → DALL-E 3 (fallback)
2026-03-15 KST: DALL-E 3 → GPT-Image-1 교체
"""
from __future__ import annotations

import asyncio
import base64
import re

import httpx

from app.config import settings


class ImageService:
    def __init__(self):
        google_key = settings.GOOGLE_API_KEY.get_secret_value() if settings.GOOGLE_API_KEY else ""
        openai_key = settings.OPENAI_API_KEY.get_secret_value() if settings.OPENAI_API_KEY else ""
        if google_key:
            self.provider = "google"
        elif openai_key:
            self.provider = "openai"
        else:
            self.provider = "none"

    def _sanitize_prompt(self, prompt: str) -> str:
        s = prompt[:1000]
        s = re.sub(r"\b(brand|logo|trademark|celebrity|famous)\b", "", s, flags=re.I)
        s = " ".join(s.split())
        return s or "digital art, clean background"

    async def generate(self, prompt: str, size: str = "1024x1024") -> dict:
        """
        Returns: {"url": str, "provider": str, "prompt": str}
        url은 base64 data URI 형식 (data:image/png;base64,...)
        """
        if self.provider == "none":
            raise ValueError("이미지 생성 API 키가 없습니다 (GOOGLE_API_KEY 또는 OPENAI_API_KEY 필요)")

        sanitized = self._sanitize_prompt(prompt)

        if self.provider == "google":
            return await self._generate_google(sanitized, prompt)
        else:
            return await self._generate_openai(sanitized, prompt, size)

    async def _generate_google(self, sanitized: str, original: str) -> dict:
        try:
            from google import genai
            from google.genai import types
            google_key = settings.GOOGLE_API_KEY.get_secret_value()
            client = genai.Client(api_key=google_key)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_images(
                    model="imagen-4.0-generate-001",
                    prompt=sanitized,
                    config=types.GenerateImagesConfig(
                        number_of_images=1,
                        aspect_ratio="1:1",
                    ),
                ),
            )
            if not response.generated_images:
                raise ValueError("No images returned from Google Imagen")
            image_bytes = response.generated_images[0].image.image_bytes
            b64 = base64.b64encode(image_bytes).decode()
            return {"url": f"data:image/png;base64,{b64}", "provider": "Google Imagen 4.0", "prompt": original}
        except Exception as e:
            # fallback to OpenAI
            openai_key = settings.OPENAI_API_KEY.get_secret_value() if settings.OPENAI_API_KEY else ""
            if openai_key:
                return await self._generate_openai(sanitized, original, "1024x1024")
            raise ValueError(f"Google Imagen 실패: {e}")

    async def _generate_openai(self, sanitized: str, original: str, size: str) -> dict:
        """GPT-Image-1 우선 시도, 실패 시 DALL-E 3 폴백"""
        from openai import AsyncOpenAI
        openai_key = settings.OPENAI_API_KEY.get_secret_value()
        client = AsyncOpenAI(api_key=openai_key)

        for model_name in ["gpt-image-1", "dall-e-3"]:
            try:
                resp = await client.images.generate(
                    model=model_name,
                    prompt=sanitized,
                    size=size,
                    quality="standard",
                    n=1,
                )
                # gpt-image-1은 b64_json 직접 반환
                if resp.data and resp.data[0].b64_json:
                    b64 = resp.data[0].b64_json
                    return {"url": f"data:image/png;base64,{b64}", "provider": model_name, "prompt": original}
                # dall-e-3는 URL 반환
                url = resp.data[0].url if resp.data else None
                if not url:
                    continue
                async with httpx.AsyncClient() as hc:
                    r = await hc.get(url)
                    r.raise_for_status()
                    b64 = base64.b64encode(r.content).decode()
                return {"url": f"data:image/png;base64,{b64}", "provider": model_name, "prompt": original}
            except Exception:
                if model_name == "dall-e-3":
                    raise
                continue
        raise ValueError("No image generated from OpenAI")


image_service = ImageService()
