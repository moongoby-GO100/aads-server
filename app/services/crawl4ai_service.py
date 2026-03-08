"""
AADS-186E-1: Crawl4AI 서비스 — JS 렌더링 포함 크롤링 (폴백)
Crawl4AI Docker 서버 (http://localhost:11235) 호출.
미설치 시 graceful 비활성화 — 에러 아님.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_CRAWL4AI_BASE = os.getenv("CRAWL4AI_BASE_URL", "http://localhost:11235")
_CRAWL4AI_TIMEOUT = 60.0  # JS 렌더링은 시간이 걸림


@dataclass
class CrawlResult:
    """Crawl4AI 크롤링 결과."""
    url: str
    content: str  # 마크다운
    word_count: int
    js_rendered: bool
    error: Optional[str] = None


class Crawl4AIService:
    """Crawl4AI Docker 서버로 JS 렌더링 포함 크롤링."""

    def __init__(self) -> None:
        self._available: Optional[bool] = None  # None = 미확인

    async def is_available(self) -> bool:
        """Crawl4AI 서버 가용 여부 확인 (캐시)."""
        if self._available is not None:
            return self._available
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{_CRAWL4AI_BASE}/health")
                self._available = resp.status_code == 200
        except Exception:
            self._available = False
        logger.info(f"crawl4ai available={self._available}")
        return self._available

    async def fetch_page(self, url: str, js_render: bool = True) -> Optional[CrawlResult]:
        """
        Crawl4AI REST API로 페이지 크롤링.
        Crawl4AI 미설치 시 None 반환 (graceful skip).

        Args:
            url: 크롤링할 URL
            js_render: JS 렌더링 여부 (기본 True)

        Returns:
            CrawlResult 또는 서버 미가용 시 None
        """
        if not await self.is_available():
            logger.debug("crawl4ai not available — skipping")
            return None

        try:
            payload = {
                "urls": url,
                "word_count_threshold": 10,
                "extract_blocks": False,
                "output_formats": ["markdown"],
            }
            if js_render:
                payload["js_code"] = None  # Playwright 기본 설정으로 렌더링

            async with httpx.AsyncClient(timeout=_CRAWL4AI_TIMEOUT) as client:
                resp = await client.post(
                    f"{_CRAWL4AI_BASE}/crawl",
                    json=payload,
                )

            if resp.status_code != 200:
                logger.warning(f"crawl4ai http {resp.status_code}: {url}")
                return CrawlResult(
                    url=url,
                    content="",
                    word_count=0,
                    js_rendered=js_render,
                    error=f"http_{resp.status_code}",
                )

            data = resp.json()
            # Crawl4AI 응답 구조: {"results": [{"markdown": "...", ...}]}
            results = data.get("results", [])
            if not results:
                return CrawlResult(url=url, content="", word_count=0, js_rendered=js_render, error="empty_result")

            first = results[0]
            content = first.get("markdown", "") or first.get("content", "")
            word_count = len(content.split())
            return CrawlResult(
                url=url,
                content=content,
                word_count=word_count,
                js_rendered=js_render,
            )

        except httpx.TimeoutException:
            logger.warning(f"crawl4ai timeout: {url}")
            return CrawlResult(url=url, content="", word_count=0, js_rendered=js_render, error="timeout")
        except Exception as e:
            logger.warning(f"crawl4ai error: {url} — {e}")
            return CrawlResult(url=url, content="", word_count=0, js_rendered=js_render, error=str(e))
