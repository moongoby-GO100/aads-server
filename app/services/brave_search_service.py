"""
AADS-185-B5: Brave Search 서비스 — Gemini Grounding 폴백
$5/1K requests, 월 $5 무료 크레딧.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


@dataclass
class SearchResult:
    """검색 결과 통합 포맷 (GeminiSearchService와 동일 인터페이스)."""
    text: str
    citations: List[dict] = field(default_factory=list)
    cost: Decimal = Decimal("0.005")  # $5/1K = $0.005/건
    model: str = "brave-search"
    error: Optional[str] = None


class BraveSearchService:
    """Brave Search API — Gemini Grounding 폴백용."""

    async def search(
        self,
        query: str,
        count: int = 5,
        freshness: Optional[str] = None,
    ) -> SearchResult:
        """
        Brave Search API로 웹 검색.

        Args:
            query: 검색 쿼리
            count: 결과 수 (기본 5, 최대 10)
            freshness: 최신성 필터 ('pd', 'pw', 'pm', 'py')

        Returns:
            SearchResult (text + citations)
        """
        if not BRAVE_API_KEY:
            return SearchResult(
                text=f"[검색 불가: BRAVE_API_KEY 미설정]\n\n"
                     f"질문: {query}\n\n"
                     f"Brave Search API 키를 설정하거나 Gemini API를 사용하세요.",
                error="BRAVE_API_KEY not set",
            )

        count = min(count, 10)
        params = {
            "q": query,
            "count": count,
            "search_lang": "ko",
            "country": "KR",
        }
        if freshness:
            params["freshness"] = freshness

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    BRAVE_SEARCH_URL,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "X-Subscription-Token": BRAVE_API_KEY,
                    },
                    params=params,
                )
                if resp.status_code != 200:
                    raise Exception(f"Brave API error {resp.status_code}")

                data = resp.json()
                return self._parse_response(query, data)

        except Exception as e:
            logger.error(f"brave_search_error: {e}")
            return SearchResult(
                text=f"[검색 오류: {e}]",
                error=str(e),
            )

    def _parse_response(self, query: str, data: dict) -> SearchResult:
        """Brave API 응답 → SearchResult 변환."""
        results = data.get("web", {}).get("results", [])
        if not results:
            return SearchResult(
                text=f"'{query}'에 대한 검색 결과가 없습니다.",
            )

        citations = []
        text_parts = [f"**{query}** 검색 결과:\n"]

        for i, item in enumerate(results[:5]):
            title = item.get("title", "")
            url = item.get("url", "")
            description = item.get("description", "")
            age = item.get("age", "")

            citations.append({
                "index": i,
                "title": title,
                "url": url,
                "snippet": description[:200],
                "age": age,
            })
            text_parts.append(
                f"\n{i+1}. **{title}**\n"
                f"   {description[:300]}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts),
            citations=citations,
            cost=Decimal("0.005"),
            model="brave-search",
        )
