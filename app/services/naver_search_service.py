"""
Naver Web Search API 서비스.
https://developers.naver.com/docs/serviceapi/search/web/web.md
일 25,000건 무료.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
NAVER_SEARCH_URL = "https://openapi.naver.com/v1/search/webkr.json"


@dataclass
class SearchResult:
    """검색 결과 통합 포맷."""
    text: str
    citations: List[dict] = field(default_factory=list)
    cost: Decimal = Decimal("0")
    model: str = "naver-search"
    error: Optional[str] = None


class NaverSearchService:
    """Naver 웹 검색 API."""

    def is_available(self) -> bool:
        return bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)

    async def search(
        self,
        query: str,
        count: int = 5,
        sort: str = "sim",
    ) -> SearchResult:
        if not self.is_available():
            return SearchResult(
                text=f"[Naver 검색 불가: API 키 미설정]",
                error="NAVER_CLIENT_ID or NAVER_CLIENT_SECRET not set",
            )

        count = min(count, 10)
        params = {
            "query": query,
            "display": count,
            "start": 1,
            "sort": sort,  # sim(정확도) | date(최신)
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    NAVER_SEARCH_URL,
                    headers={
                        "X-Naver-Client-Id": NAVER_CLIENT_ID,
                        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
                    },
                    params=params,
                )
                if resp.status_code != 200:
                    raise Exception(f"Naver API error {resp.status_code}: {resp.text[:200]}")

                data = resp.json()
                return self._parse_response(query, data)

        except Exception as e:
            logger.error(f"naver_search_error: {e}")
            return SearchResult(text=f"[Naver 검색 오류: {e}]", error=str(e))

    def _parse_response(self, query: str, data: dict) -> SearchResult:
        items = data.get("items", [])
        if not items:
            return SearchResult(text=f"'{query}'에 대한 Naver 검색 결과가 없습니다.")

        citations = []
        text_parts = [f"**{query}** Naver 검색 결과:\n"]

        for i, item in enumerate(items[:5]):
            title = _strip_html(item.get("title", ""))
            url = item.get("link", "")
            description = _strip_html(item.get("description", ""))

            citations.append({
                "index": i,
                "title": title,
                "url": url,
                "snippet": description[:200],
                "favicon": f"https://www.google.com/s2/favicons?domain={url}",
            })
            text_parts.append(
                f"\n{i+1}. **{title}**\n"
                f"   {description[:300]}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts),
            citations=citations,
            cost=Decimal("0"),
            model="naver-search",
        )


def _strip_html(text: str) -> str:
    """Remove HTML tags from Naver API response."""
    import re
    return re.sub(r"<[^>]+>", "", text)
