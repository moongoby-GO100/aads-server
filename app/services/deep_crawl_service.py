"""
AADS-186E-1: DeepCrawl 서비스 — 검색 → 다중 크롤링 → 종합 요약 파이프라인
Step 1: Brave 검색 → 상위 URL max_pages개
Step 2: Jina Reader 병렬 크롤링 (asyncio.gather), 실패 시 Crawl4AI 폴백
Step 3: Gemini Flash로 각 페이지 요약 (5000토큰)
Step 4: Claude Sonnet으로 종합 분석
Step 5: 인용 포함 최종 결과 반환
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://aads-litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

_MAX_TOTAL_TOKENS = 15000
from app.core.token_utils import CHARS_PER_TOKEN as _CPT
_MAX_PAGE_SUMMARY_CHARS = 5000 * _CPT  # 5000 토큰 → 문자 변환


@dataclass
class CrawledPage:
    """크롤링된 단일 페이지 정보."""
    url: str
    title: str
    content: str        # 마크다운 원문 (요약 전)
    summary: str = ""   # Gemini 요약
    error: Optional[str] = None


@dataclass
class DeepCrawlResult:
    """DeepCrawl 파이프라인 최종 결과."""
    query: str
    synthesis: str                       # Claude 종합 분석
    citations: List[dict] = field(default_factory=list)
    pages_crawled: int = 0
    pages_failed: int = 0
    total_cost_usd: float = 0.028        # 검색+크롤링+요약+종합 예상 합산
    error: Optional[str] = None


class DeepCrawlService:
    """검색 → 다중 크롤링 → 종합 요약 파이프라인."""

    async def research_crawl(
        self,
        query: str,
        max_pages: int = 5,
        summarize: bool = True,
    ) -> DeepCrawlResult:
        """
        검색 후 다중 URL 크롤링 및 종합 분석.

        Args:
            query: 검색 쿼리
            max_pages: 크롤링할 최대 페이지 수 (기본 5)
            summarize: 종합 요약 수행 여부 (기본 True)

        Returns:
            DeepCrawlResult (synthesis + citations)
        """
        max_pages = min(max_pages, 10)

        # Step 1: Brave 검색
        urls = await self._search(query, max_pages)
        if not urls:
            return DeepCrawlResult(
                query=query,
                synthesis=f"'{query}'에 대한 검색 결과를 찾을 수 없습니다.",
                error="no_search_results",
            )

        # Step 2: 병렬 크롤링
        pages = await self._crawl_all(urls)
        crawled = [p for p in pages if not p.error and p.content]
        failed = len(pages) - len(crawled)

        if not crawled:
            return DeepCrawlResult(
                query=query,
                synthesis=f"'{query}' 검색 결과 URL을 크롤링하는 데 실패했습니다.",
                pages_crawled=0,
                pages_failed=failed,
                error="all_crawls_failed",
            )

        # Step 3: 각 페이지 요약 (Gemini Flash)
        if summarize:
            await self._summarize_pages(crawled)

        # Step 4: 종합 분석 (Claude Sonnet) 또는 단순 병합
        if summarize:
            synthesis = await self._synthesize(query, crawled)
        else:
            synthesis = self._merge_contents(query, crawled)

        # Step 5: 인용 생성
        citations = [
            {
                "index": i,
                "title": p.title,
                "url": p.url,
                "excerpt": (p.summary or p.content)[:300],
            }
            for i, p in enumerate(crawled)
        ]

        return DeepCrawlResult(
            query=query,
            synthesis=synthesis,
            citations=citations,
            pages_crawled=len(crawled),
            pages_failed=failed,
        )

    # ── 내부 메서드 ──────────────────────────────────────────────────────────

    async def _search(self, query: str, count: int) -> List[str]:
        """Brave 검색 → URL 목록 반환."""
        if not BRAVE_API_KEY:
            logger.warning("deep_crawl: BRAVE_API_KEY 미설정 — 검색 불가")
            return []

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    _BRAVE_SEARCH_URL,
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": BRAVE_API_KEY,
                    },
                    params={"q": query, "count": count, "search_lang": "ko"},
                )
                if resp.status_code != 200:
                    logger.warning(f"deep_crawl brave {resp.status_code}")
                    return []

                data = resp.json()
                results = data.get("web", {}).get("results", [])
                return [r.get("url", "") for r in results if r.get("url")]

        except Exception as e:
            logger.error(f"deep_crawl search error: {e}")
            return []

    async def _crawl_all(self, urls: List[str]) -> List[CrawledPage]:
        """URL 목록 병렬 크롤링 (Jina → Crawl4AI 폴백)."""
        from app.services.jina_reader_service import JinaReaderService
        from app.services.crawl4ai_service import Crawl4AIService

        jina = JinaReaderService()
        crawl4ai = Crawl4AIService()

        async def _fetch_one(url: str) -> CrawledPage:
            # Jina Reader 시도
            result = await jina.read_url(url, timeout=30, max_tokens=8000)
            if result and result.content:
                return CrawledPage(
                    url=url,
                    title=result.title,
                    content=result.content,
                )
            # Crawl4AI 폴백
            c4 = await crawl4ai.fetch_page(url)
            if c4 and c4.content:
                return CrawledPage(
                    url=url,
                    title=url,
                    content=c4.content,
                )
            return CrawledPage(url=url, title=url, content="", error="crawl_failed")

        tasks = [_fetch_one(u) for u in urls]
        return await asyncio.gather(*tasks, return_exceptions=False)

    async def _summarize_pages(self, pages: List[CrawledPage]) -> None:
        """각 페이지를 Gemini Flash로 요약 (in-place 수정)."""
        tasks = [self._summarize_one(p) for p in pages]
        summaries = await asyncio.gather(*tasks, return_exceptions=True)
        for page, summary in zip(pages, summaries):
            if isinstance(summary, str):
                page.summary = summary

    async def _summarize_one(self, page: CrawledPage) -> str:
        """단일 페이지를 Gemini Flash로 5000토큰 이내 요약."""
        content = page.content[:_MAX_PAGE_SUMMARY_CHARS]
        prompt = (
            f"다음 웹페이지 내용을 2000자 이내로 핵심만 요약하세요.\n"
            f"URL: {page.url}\n\n"
            f"---\n{content}\n---\n\n"
            f"한국어로 요약:"
        )
        return await self._llm_call("gemini-flash", prompt, max_tokens=800)

    async def _synthesize(self, query: str, pages: List[CrawledPage]) -> str:
        """Claude Sonnet으로 전체 요약 종합 분석."""
        summaries_text = "\n\n".join(
            f"[{i+1}] {p.title} ({p.url})\n{p.summary or p.content[:1000]}"
            for i, p in enumerate(pages)
        )
        # 최대 토큰 제한
        if len(summaries_text) > _MAX_TOTAL_TOKENS * _CPT:
            summaries_text = summaries_text[:_MAX_TOTAL_TOKENS * _CPT] + "\n...[내용 절삭됨]"

        prompt = (
            f"다음은 '{query}' 주제로 검색·크롤링한 {len(pages)}개 페이지의 요약입니다.\n\n"
            f"{summaries_text}\n\n"
            f"---\n"
            f"위 내용을 종합하여 '{query}'에 대한 심층 분석 보고서를 작성하세요.\n"
            f"형식: 마크다운, 핵심 인사이트 + 주요 발견사항 + 결론 포함."
        )
        return await self._llm_call("claude-sonnet", prompt, max_tokens=2000)

    def _merge_contents(self, query: str, pages: List[CrawledPage]) -> str:
        """요약 없이 단순 병합 (summarize=False 시)."""
        parts = [f"## '{query}' 크롤링 결과 ({len(pages)}개 페이지)\n"]
        for i, p in enumerate(pages):
            parts.append(f"\n### [{i+1}] {p.title}\n출처: {p.url}\n\n{p.content[:2000]}")
        return "\n".join(parts)

    async def _llm_call(self, model: str, prompt: str, max_tokens: int = 1000) -> str:
        """LiteLLM 경유 LLM 호출."""
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(
                    f"{LITELLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": 0.3,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                logger.warning(f"deep_crawl llm {resp.status_code}: {model}")
                return ""
        except Exception as e:
            logger.error(f"deep_crawl llm_call error: {e}")
            return ""
