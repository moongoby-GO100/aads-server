"""
Naver Search API 통합 서비스 (비로그인 오픈 API).
https://developers.naver.com/docs/serviceapi/search/
일 25,000건 무료.

지원 검색 타입:
  webkr(웹문서), blog(블로그), news(뉴스), kin(지식iN),
  encyc(백과사전), book(책), image(이미지), shop(쇼핑),
  cafearticle(카페글), doc(전문자료), local(지역)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
NAVER_API_BASE = "https://openapi.naver.com/v1/search"

# 검색 타입별 엔드포인트 및 메타 정보
SEARCH_TYPES: Dict[str, dict] = {
    "webkr":       {"label": "웹문서",   "max_display": 100},
    "blog":        {"label": "블로그",   "max_display": 100},
    "news":        {"label": "뉴스",     "max_display": 100},
    "kin":         {"label": "지식iN",   "max_display": 100},
    "encyc":       {"label": "백과사전", "max_display": 100},
    "book":        {"label": "책",       "max_display": 100},
    "image":       {"label": "이미지",   "max_display": 100},
    "shop":        {"label": "쇼핑",     "max_display": 100},
    "cafearticle": {"label": "카페글",   "max_display": 100},
    "doc":         {"label": "전문자료", "max_display": 100},
    "local":       {"label": "지역",     "max_display": 5},
}


@dataclass
class SearchResult:
    """검색 결과 통합 포맷."""
    text: str
    citations: List[dict] = field(default_factory=list)
    cost: Decimal = Decimal("0")
    model: str = "naver-search"
    error: Optional[str] = None
    search_type: str = "webkr"


class NaverSearchService:
    """Naver 검색 API — 전체 검색 타입 지원."""

    def is_available(self) -> bool:
        return bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)

    async def search(
        self,
        query: str,
        search_type: str = "webkr",
        count: int = 5,
        start: int = 1,
        sort: str = "sim",
    ) -> SearchResult:
        """
        네이버 검색 실행.

        Args:
            query: 검색어
            search_type: webkr|blog|news|kin|encyc|book|image|shop|cafearticle|doc|local
            count: 결과 수 (1~100, local은 1~5)
            start: 시작 위치 (1~1000)
            sort: sim(정확도) | date(최신) — shop은 sim|date|asc|dsc
        """
        if not self.is_available():
            return SearchResult(
                text="[Naver 검색 불가: API 키 미설정]",
                error="NAVER_CLIENT_ID or NAVER_CLIENT_SECRET not set",
            )

        if search_type not in SEARCH_TYPES:
            search_type = "webkr"

        meta = SEARCH_TYPES[search_type]
        count = max(1, min(count, meta["max_display"]))

        url = f"{NAVER_API_BASE}/{search_type}.json"
        params = {
            "query": query,
            "display": count,
            "start": max(1, min(start, 1000)),
            "sort": sort,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url,
                    headers={
                        "X-Naver-Client-Id": NAVER_CLIENT_ID,
                        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
                    },
                    params=params,
                )
                if resp.status_code != 200:
                    raise Exception(f"Naver API error {resp.status_code}: {resp.text[:200]}")

                data = resp.json()
                return self._parse_response(query, search_type, data)

        except Exception as e:
            logger.error(f"naver_search_error ({search_type}): {e}")
            return SearchResult(
                text=f"[Naver {meta['label']} 검색 오류: {e}]",
                error=str(e),
                search_type=search_type,
            )

    async def multi_search(
        self,
        query: str,
        types: List[str] | None = None,
        count: int = 3,
    ) -> SearchResult:
        """
        여러 검색 타입을 동시에 조회해 통합 결과 반환.
        기본: webkr + blog + news + kin
        """
        if types is None:
            types = ["webkr", "blog", "news", "kin"]

        all_text: list[str] = []
        all_citations: list[dict] = []
        errors: list[str] = []

        import asyncio
        tasks = [self.search(query, st, count=count) for st in types]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for st, result in zip(types, results):
            if isinstance(result, Exception):
                errors.append(f"{st}: {result}")
                continue
            if result.error:
                errors.append(f"{st}: {result.error}")
                continue
            all_text.append(result.text)
            all_citations.extend(result.citations)

        if not all_text:
            return SearchResult(
                text=f"'{query}' Naver 통합 검색 결과 없음.",
                error="; ".join(errors) if errors else None,
            )

        return SearchResult(
            text="\n\n---\n\n".join(all_text),
            citations=all_citations,
            cost=Decimal("0"),
            model="naver-multi-search",
        )

    # ── 타입별 파서 ──

    def _parse_response(self, query: str, search_type: str, data: dict) -> SearchResult:
        items = data.get("items", [])
        label = SEARCH_TYPES.get(search_type, {}).get("label", search_type)

        if not items:
            return SearchResult(
                text=f"'{query}'에 대한 Naver {label} 검색 결과가 없습니다.",
                search_type=search_type,
            )

        parser = getattr(self, f"_parse_{search_type}", self._parse_generic)
        return parser(query, label, items, search_type)

    def _parse_generic(self, query: str, label: str, items: list, search_type: str) -> SearchResult:
        """webkr, kin, encyc, doc, cafearticle 공통 파서."""
        citations = []
        text_parts = [f"**{query}** Naver {label} 검색 결과:\n"]

        for i, item in enumerate(items[:10]):
            title = _strip_html(item.get("title", ""))
            url = item.get("link", "")
            desc = _strip_html(item.get("description", ""))

            citations.append({
                "index": i, "title": title, "url": url,
                "snippet": desc[:200],
                "favicon": f"https://www.google.com/s2/favicons?domain={url}",
            })
            text_parts.append(
                f"\n{i+1}. **{title}**\n"
                f"   {desc[:300]}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts), citations=citations,
            model=f"naver-{search_type}", search_type=search_type,
        )

    # webkr — 웹문서
    _parse_webkr = _parse_generic

    # kin — 지식iN
    _parse_kin = _parse_generic

    # encyc — 백과사전
    _parse_encyc = _parse_generic

    # doc — 전문자료
    _parse_doc = _parse_generic

    # cafearticle — 카페글
    def _parse_cafearticle(self, query: str, label: str, items: list, search_type: str) -> SearchResult:
        citations = []
        text_parts = [f"**{query}** Naver {label} 검색 결과:\n"]

        for i, item in enumerate(items[:10]):
            title = _strip_html(item.get("title", ""))
            url = item.get("link", "")
            desc = _strip_html(item.get("description", ""))
            cafe = item.get("cafename", "")

            citations.append({
                "index": i, "title": title, "url": url,
                "snippet": desc[:200], "cafe": cafe,
                "favicon": f"https://www.google.com/s2/favicons?domain={url}",
            })
            text_parts.append(
                f"\n{i+1}. **{title}** ({cafe})\n"
                f"   {desc[:300]}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts), citations=citations,
            model="naver-cafearticle", search_type=search_type,
        )

    # blog — 블로그
    def _parse_blog(self, query: str, label: str, items: list, search_type: str) -> SearchResult:
        citations = []
        text_parts = [f"**{query}** Naver {label} 검색 결과:\n"]

        for i, item in enumerate(items[:10]):
            title = _strip_html(item.get("title", ""))
            url = item.get("link", "")
            desc = _strip_html(item.get("description", ""))
            blogger = item.get("bloggername", "")
            postdate = item.get("postdate", "")  # YYYYMMDD

            citations.append({
                "index": i, "title": title, "url": url,
                "snippet": desc[:200], "author": blogger, "date": postdate,
                "favicon": f"https://www.google.com/s2/favicons?domain={url}",
            })
            text_parts.append(
                f"\n{i+1}. **{title}** — {blogger} ({postdate})\n"
                f"   {desc[:300]}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts), citations=citations,
            model="naver-blog", search_type=search_type,
        )

    # news — 뉴스
    def _parse_news(self, query: str, label: str, items: list, search_type: str) -> SearchResult:
        citations = []
        text_parts = [f"**{query}** Naver {label} 검색 결과:\n"]

        for i, item in enumerate(items[:10]):
            title = _strip_html(item.get("title", ""))
            url = item.get("originallink", "") or item.get("link", "")
            naver_url = item.get("link", "")
            desc = _strip_html(item.get("description", ""))
            pub_date = item.get("pubDate", "")

            citations.append({
                "index": i, "title": title, "url": url,
                "naver_url": naver_url,
                "snippet": desc[:200], "date": pub_date,
                "favicon": f"https://www.google.com/s2/favicons?domain={url}",
            })
            text_parts.append(
                f"\n{i+1}. **{title}** ({pub_date})\n"
                f"   {desc[:300]}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts), citations=citations,
            model="naver-news", search_type=search_type,
        )

    # book — 책
    def _parse_book(self, query: str, label: str, items: list, search_type: str) -> SearchResult:
        citations = []
        text_parts = [f"**{query}** Naver {label} 검색 결과:\n"]

        for i, item in enumerate(items[:10]):
            title = _strip_html(item.get("title", ""))
            url = item.get("link", "")
            author = item.get("author", "")
            publisher = item.get("publisher", "")
            desc = _strip_html(item.get("description", ""))
            price = item.get("discount", "")  # 판매가
            image = item.get("image", "")
            isbn = item.get("isbn", "")
            pubdate = item.get("pubdate", "")

            citations.append({
                "index": i, "title": title, "url": url,
                "snippet": desc[:200], "author": author, "publisher": publisher,
                "price": price, "image": image, "isbn": isbn, "date": pubdate,
                "favicon": "https://www.google.com/s2/favicons?domain=book.naver.com",
            })
            text_parts.append(
                f"\n{i+1}. **{title}** — {author} / {publisher}\n"
                f"   {desc[:200]}\n"
                f"   가격: {price}원 | ISBN: {isbn}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts), citations=citations,
            model="naver-book", search_type=search_type,
        )

    # image — 이미지
    def _parse_image(self, query: str, label: str, items: list, search_type: str) -> SearchResult:
        citations = []
        text_parts = [f"**{query}** Naver {label} 검색 결과:\n"]

        for i, item in enumerate(items[:10]):
            title = _strip_html(item.get("title", ""))
            url = item.get("link", "")  # 원본 이미지 URL
            thumbnail = item.get("thumbnail", "")
            size_w = item.get("sizewidth", "")
            size_h = item.get("sizeheight", "")

            citations.append({
                "index": i, "title": title, "url": url,
                "thumbnail": thumbnail, "width": size_w, "height": size_h,
            })
            text_parts.append(
                f"\n{i+1}. **{title}** ({size_w}x{size_h})\n"
                f"   ![{title}]({thumbnail})\n"
                f"   원본: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts), citations=citations,
            model="naver-image", search_type=search_type,
        )

    # shop — 쇼핑
    def _parse_shop(self, query: str, label: str, items: list, search_type: str) -> SearchResult:
        citations = []
        text_parts = [f"**{query}** Naver {label} 검색 결과:\n"]

        for i, item in enumerate(items[:10]):
            title = _strip_html(item.get("title", ""))
            url = item.get("link", "")
            image = item.get("image", "")
            lprice = item.get("lprice", "")
            hprice = item.get("hprice", "")
            mall = item.get("mallName", "")
            brand = item.get("brand", "")
            maker = item.get("maker", "")
            cat1 = item.get("category1", "")
            cat2 = item.get("category2", "")

            price_str = f"{int(lprice):,}원" if lprice else "가격 미정"
            if hprice:
                price_str += f" ~ {int(hprice):,}원"

            citations.append({
                "index": i, "title": title, "url": url,
                "snippet": f"{brand or maker} | {mall} | {price_str}",
                "image": image, "price": lprice, "mall": mall,
                "category": f"{cat1} > {cat2}",
                "favicon": f"https://www.google.com/s2/favicons?domain={url}",
            })
            text_parts.append(
                f"\n{i+1}. **{title}**\n"
                f"   {price_str} | {mall or '네이버쇼핑'}"
                f"{f' | {brand}' if brand else ''}\n"
                f"   카테고리: {cat1}{f' > {cat2}' if cat2 else ''}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts), citations=citations,
            model="naver-shop", search_type=search_type,
        )

    # local — 지역
    def _parse_local(self, query: str, label: str, items: list, search_type: str) -> SearchResult:
        citations = []
        text_parts = [f"**{query}** Naver {label} 검색 결과:\n"]

        for i, item in enumerate(items[:5]):
            title = _strip_html(item.get("title", ""))
            url = item.get("link", "")
            category = item.get("category", "")
            desc = _strip_html(item.get("description", ""))
            tel = item.get("telephone", "")
            address = item.get("address", "")
            road = item.get("roadAddress", "")

            citations.append({
                "index": i, "title": title, "url": url,
                "snippet": f"{road or address} | {tel}",
                "category": category, "address": road or address, "tel": tel,
                "favicon": "https://www.google.com/s2/favicons?domain=map.naver.com",
            })
            text_parts.append(
                f"\n{i+1}. **{title}** [{category}]\n"
                f"   주소: {road or address}\n"
                f"   전화: {tel}\n"
                f"   출처: {url}"
            )

        return SearchResult(
            text="\n".join(text_parts), citations=citations,
            model="naver-local", search_type=search_type,
        )


def _strip_html(text: str) -> str:
    """Remove HTML tags from Naver API response."""
    return re.sub(r"<[^>]+>", "", text)
