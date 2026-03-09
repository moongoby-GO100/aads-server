"""
Kakao (Daum) Web Search API 서비스.
https://developers.kakao.com/docs/latest/ko/daum-search/dev-guide
무료 (일 제한 있음).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")
KAKAO_SEARCH_URL = "https://dapi.kakao.com/v2/search/web"


@dataclass
class SearchResult:
    """검색 결과 통합 포맷."""
    text: str
    citations: List[dict] = field(default_factory=list)
    cost: Decimal = Decimal("0")
    model: str = "kakao-search"
    error: Optional[str] = None


class KakaoSearchService:
    """Kakao (Daum) 웹 검색 API."""

    def is_available(self) -> bool:
        return bool(KAKAO_REST_API_KEY)

    async def search(
        self,
        query: str,
        count: int = 5,
        sort: str = "accuracy",
    ) -> SearchResult:
        if not self.is_available():
            return SearchResult(
                text=f"[Kakao 검색 불가: API 키 미설정]",
                error="KAKAO_REST_API_KEY not set",
            )

        count = min(count, 10)
        params = {
            "query": query,
            "sort": sort,  # accuracy | recency
            "page": 1,
            "size": count,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    KAKAO_SEARCH_URL,
                    headers={
                        "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}",
                    },
                    params=params,
                )
                if resp.status_code != 200:
                    raise Exception(f"Kakao API error {resp.status_code}: {resp.text[:200]}")

                data = resp.json()
                return self._parse_response(query, data)

        except Exception as e:
            logger.error(f"kakao_search_error: {e}")
            return SearchResult(text=f"[Kakao 검색 오류: {e}]", error=str(e))

    def _parse_response(self, query: str, data: dict) -> SearchResult:
        documents = data.get("documents", [])
        if not documents:
            return SearchResult(text=f"'{query}'에 대한 Kakao 검색 결과가 없습니다.")

        citations = []
        text_parts = [f"**{query}** Kakao 검색 결과:\n"]

        for i, doc in enumerate(documents[:5]):
            title = _strip_html(doc.get("title", ""))
            url = doc.get("url", "")
            contents = _strip_html(doc.get("contents", ""))
            datetime_str = doc.get("datetime", "")

            citations.append({
                "index": i,
                "title": title,
                "url": url,
                "snippet": contents[:200],
                "datetime": datetime_str,
                "favicon": f"https://www.google.com/s2/favicons?domain={url}",
            })
            text_parts.append(
                f"\n{i+1}. **{title}**\n"
                f"   {contents[:300]}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts),
            citations=citations,
            cost=Decimal("0"),
            model="kakao-search",
        )


def _strip_html(text: str) -> str:
    """Remove HTML tags from Kakao API response."""
    import re
    return re.sub(r"<[^>]+>", "", text)
