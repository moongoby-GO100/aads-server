"""
AADS-186E-1: Jina Reader 서비스 — URL → 클린 마크다운 변환
무료 Jina Reader API (r.jina.ai) 사용, 타임아웃 30초, 재시도 1회
max_content_tokens: 25000 초과 시 truncate
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 약 4자 = 1토큰 (보수적 추정)
_CHARS_PER_TOKEN = 4
_DEFAULT_MAX_TOKENS = 25000
_JINA_BASE = "https://r.jina.ai"


@dataclass
class JinaResult:
    """Jina Reader API 응답 결과."""
    title: str
    content: str  # 마크다운
    word_count: int
    source_url: str
    truncated: bool = False
    error: Optional[str] = None


class JinaReaderService:
    """Jina Reader API로 URL → 클린 마크다운 변환."""

    async def read_url(self, url: str, timeout: int = 30, max_tokens: int = _DEFAULT_MAX_TOKENS) -> Optional[JinaResult]:
        """
        Jina Reader API로 URL 내용을 마크다운으로 변환.

        Args:
            url: 크롤링할 URL
            timeout: HTTP 타임아웃 (초, 기본 30)
            max_tokens: 최대 토큰 수 (기본 25000)

        Returns:
            JinaResult 또는 실패 시 None (crawl4ai 폴백 트리거)
        """
        jina_url = f"{_JINA_BASE}/{url}"
        max_chars = max_tokens * _CHARS_PER_TOKEN

        # 1차 시도
        result = await self._fetch(jina_url, timeout)
        if result is None:
            # 재시도 1회
            logger.info(f"jina_reader retry: {url}")
            result = await self._fetch(jina_url, timeout)

        if result is None:
            return None

        raw_content, title = result

        # 토큰 제한 초과 시 절삭
        truncated = False
        if len(raw_content) > max_chars:
            raw_content = raw_content[:max_chars] + "\n\n[내용 절삭됨]"
            truncated = True

        word_count = len(raw_content.split())
        return JinaResult(
            title=title,
            content=raw_content,
            word_count=word_count,
            source_url=url,
            truncated=truncated,
        )

    async def _fetch(self, jina_url: str, timeout: int) -> Optional[tuple[str, str]]:
        """실제 HTTP 요청. (content, title) 반환 또는 None."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    jina_url,
                    headers={
                        "Accept": "text/markdown",
                        "User-Agent": "AADS-JinaReader/1.0",
                        "X-Return-Format": "markdown",
                    },
                    follow_redirects=True,
                )
                if resp.status_code != 200:
                    logger.warning(f"jina_reader http {resp.status_code}: {jina_url}")
                    return None

                content = resp.text
                title = self._extract_title(content, jina_url)
                return content, title

        except httpx.TimeoutException:
            logger.warning(f"jina_reader timeout: {jina_url}")
            return None
        except Exception as e:
            logger.warning(f"jina_reader error: {jina_url} — {e}")
            return None

    def _extract_title(self, content: str, fallback_url: str) -> str:
        """마크다운 첫 H1 헤더를 제목으로 추출."""
        for line in content.splitlines()[:20]:
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return fallback_url
