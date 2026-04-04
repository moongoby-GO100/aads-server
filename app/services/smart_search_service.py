"""
AADS Smart Search Service — 3단계 동적 검색 파이프라인
복잡도(SIMPLE/MEDIUM/DEEP)에 따라 검색 수와 크롤링 수를 동적으로 조정.
- SIMPLE: 검색 20개, 크롤링 0개 (snippet만, <2초)
- MEDIUM: 검색 50개, 크롤링 5개 (Jina→Crawl4AI 폴백, ~5초)
- DEEP:   검색 100개, 크롤링 15개 (Jina→Crawl4AI 폴백, ~12초)
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.core.anthropic_client import call_llm_with_fallback

logger = logging.getLogger(__name__)


def detect_query_complexity(query: str) -> str:
    score = 0
    # 길이 점수
    if len(query) > 40: score += 1
    if len(query) > 100: score += 1
    # 분석/종합 키워드 (최대 +2)
    analysis_kw = ["분석", "정리", "비교", "종합", "현황", "전망", "조사", "평가",
                   "영향", "추이", "연구", "논문", "학술", "심층", "종합적", "자세히",
                   "analyze", "comprehensive", "research", "compare"]
    analysis_hits = sum(1 for kw in analysis_kw if kw in query)
    score += min(analysis_hits, 2)
    # 다중 요구 패턴
    multi_kw = [" 및 ", "그리고", "와 함께", "주요 내용과", "장단점"]
    if any(kw in query for kw in multi_kw): score += 1
    # 단순 사실 키워드 (최소 -2)
    simple_kw = ["가격", "현재", "오늘", "최신", "몇", "언제", "어디", "누구",
                 "날씨", "환율", "시간", "몇시", "얼마"]
    simple_hits = sum(1 for kw in simple_kw if kw in query)
    score -= min(simple_hits, 2)
    if score <= 0: return "SIMPLE"
    elif score == 1: return "MEDIUM"
    else: return "DEEP"


async def _select_urls_by_llm(
    query: str,
    candidates: List[Dict[str, Any]],  # [{"url": ..., "title": ..., "snippet": ...}, ...]
    max_select: int,
) -> List[str]:
    """스니펫 목록을 LLM에게 보여주고 크롤링 필요 URL 선택"""
    if len(candidates) <= max_select:
        return [c["url"] for c in candidates]

    # 번호 붙인 검색 결과 목록 구성
    lines = []
    for i, c in enumerate(candidates, 1):
        title = c.get("title", "")
        url = c["url"]
        snippet = c.get("snippet", "")[:200]
        lines.append(f"{i}. [{title}] {url}\n   {snippet}")

    results_text = "\n".join(lines)
    prompt = (
        f"질문: {query}\n\n"
        f"아래 검색 결과 중 질문에 답하기 위해 원문 전체를 읽어야 할 URL을 최대 {max_select}개 선택하세요.\n"
        f"snippet만으로 답할 수 있는 결과는 제외하세요.\n"
        f"JSON 배열로만 응답: [\"url1\", \"url2\", ...]\n\n"
        f"검색 결과:\n{results_text}"
    )

    try:
        resp = await asyncio.wait_for(
            call_llm_with_fallback(
                prompt=prompt,
                model="qwen-turbo",
                max_tokens=200,
            ),
            timeout=10,
        )
        if resp:
            m = re.search(r'\[.*?\]', resp, re.DOTALL)
            if m:
                selected = json.loads(m.group())
                if isinstance(selected, list):
                    # 실제 candidates에 있는 URL만 필터링
                    valid_urls = {c["url"] for c in candidates}
                    filtered = [u for u in selected if u in valid_urls]
                    logger.info(
                        f"llm_url_select: query={query[:30]}, "
                        f"candidates={len(candidates)}, selected={len(filtered)}"
                    )
                    return filtered[:max_select]
    except Exception as e:
        logger.warning(f"llm_url_select_fallback: {e}")

    # fallback: score 기반 상위 max_select개
    return [c["url"] for c in candidates[:max_select]]


async def _crawl_url(url: str, max_tokens: int) -> Optional[Dict[str, Any]]:
    # 1순위: Jina Reader
    try:
        from app.services.jina_reader_service import JinaReaderService
        jina = JinaReaderService()
        result = await jina.read_url(url, timeout=6, max_tokens=max_tokens)
        if result and result.content and not result.error:
            return {"url": url, "content": result.content, "source": "jina"}
    except Exception as e:
        logger.debug(f"jina_failed url={url}: {e}")

    # 2순위: Crawl4AI
    try:
        from app.services.crawl4ai_service import Crawl4AIService
        c4 = Crawl4AIService()
        if await c4.is_available():
            result = await c4.fetch_page(url, js_render=False)
            if result and result.content and not result.error:
                # max_tokens 기준으로 truncate
                try:
                    from app.core.token_utils import CHARS_PER_TOKEN
                except Exception:
                    CHARS_PER_TOKEN = 2
                max_chars = max_tokens * CHARS_PER_TOKEN
                content = result.content[:max_chars]
                return {"url": url, "content": content, "source": "crawl4ai"}
    except Exception as e:
        logger.debug(f"crawl4ai_failed url={url}: {e}")

    return None


async def smart_search(
    query: str,
    complexity: Optional[str] = None,
    naver_type: str = "",
) -> Dict[str, Any]:
    # Stage 1: 복잡도 결정 및 파라미터 설정
    if complexity is None:
        complexity = detect_query_complexity(query)

    count_map = {"SIMPLE": 20, "MEDIUM": 50, "DEEP": 100}
    crawl_count_map = {"SIMPLE": 0, "MEDIUM": 5, "DEEP": 15}
    max_tokens_map = {"SIMPLE": 8000, "MEDIUM": 25000, "DEEP": 50000}
    gather_timeout_map = {"SIMPLE": 0, "MEDIUM": 20, "DEEP": 45}

    search_count = count_map.get(complexity, 20)
    crawl_count = crawl_count_map.get(complexity, 0)
    max_tokens = max_tokens_map.get(complexity, 8000)
    gather_timeout = gather_timeout_map.get(complexity, 12)

    from app.services.searxng_search_service import search_searxng
    sxng = await search_searxng(query, categories="general", count=search_count)

    if sxng.get("error") or not sxng.get("results"):
        return {"error": sxng.get("error", "검색 결과 없음"), "results": [],
                "crawled": [], "complexity": complexity, "crawl_count": 0,
                "formatted_text": "", "citations": []}

    results = sxng["results"]

    # Stage 2: LLM 판단 기반 URL 선택 (MEDIUM/DEEP만, SIMPLE은 crawl_count=0으로 자연히 건너뜀)
    crawled_data: List[Dict[str, Any]] = []
    if crawl_count > 0:
        from urllib.parse import urlparse
        # 크롤링 후보 준비 (score 상위 crawl_count*3개로 제한, LLM 프롬프트 길이 제한)
        candidate_pool = sorted(
            [r for r in results if r.get("url")],
            key=lambda x: float(x.get("score", 0)),
            reverse=True
        )[:crawl_count * 3]  # LLM에게 보낼 최대 후보 수

        candidates_for_llm = [
            {"url": r["url"], "title": r.get("title", ""), "snippet": r.get("content", "")[:200]}
            for r in candidate_pool
        ]

        # LLM으로 URL 선택
        candidate_urls = await _select_urls_by_llm(query, candidates_for_llm, crawl_count)

        # Stage 3: 병렬 크롤링
        try:
            raw = await asyncio.wait_for(
                asyncio.gather(*[_crawl_url(u, max_tokens) for u in candidate_urls],
                               return_exceptions=True),
                timeout=gather_timeout
            )
            crawled_data = [r for r in raw if isinstance(r, dict) and r and r.get("content")]
        except asyncio.TimeoutError:
            logger.warning(f"smart_search_crawl_timeout: query={query[:50]}, complexity={complexity}")
        except Exception as e:
            logger.warning(f"smart_search_crawl_error: {e}")

    # Stage 4: formatted_text 조합
    crawled_by_url = {c["url"]: c["content"] for c in crawled_data}

    text_parts: List[str] = []
    citations: List[Dict[str, str]] = []
    for r in results[:max(search_count, len(results))]:
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("content", "")
        if not title and not snippet:
            continue
        if url in crawled_by_url:
            body = crawled_by_url[url]
            text_parts.append(f"**{title}**\n출처: {url}\n\n{body}")
        else:
            if snippet:
                text_parts.append(f"**{title}**\n{snippet}")
        if url:
            citations.append({"title": title, "url": url})

    formatted_text = "\n\n---\n\n".join(text_parts)

    return {
        "results": results,
        "crawled": crawled_data,
        "query": query,
        "complexity": complexity,
        "crawl_count": len(crawled_data),
        "formatted_text": formatted_text,
        "citations": citations[:20],  # 최대 20개
    }
