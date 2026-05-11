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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.anthropic_client import call_background_llm, call_llm_with_fallback

logger = logging.getLogger(__name__)

_TRACKING_QUERY_KEYS = frozenset({
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "source", "spm",
    "trk", "utm_campaign", "utm_content", "utm_id", "utm_medium", "utm_source", "utm_term",
})
_MATCH_RESULT_LIMIT = 10
_MATCH_CRAWL_LIMIT = 6
_MATCH_BODY_MAX_TOKENS = 2500
_MATCH_SYNTHESIS_TIMEOUT = 18.0
_MATCH_DEFAULT_SYNTHESIS_MODEL = "gpt-5.5"


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
            call_background_llm(
                prompt=prompt,
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
    return await _crawl_url_with_limits(url, max_tokens=max_tokens)


async def _crawl_url_with_limits(
    url: str,
    *,
    max_tokens: int,
    jina_timeout: int = 6,
    crawl_timeout: float = 8.0,
) -> Optional[Dict[str, Any]]:
    # 1순위: Jina Reader
    try:
        from app.services.jina_reader_service import JinaReaderService
        jina = JinaReaderService()
        result = await jina.read_url(url, timeout=jina_timeout, max_tokens=max_tokens)
        if result and result.content and not result.error:
            return {
                "url": url,
                "title": result.title,
                "content": result.content,
                "truncated": result.truncated,
                "source": "jina",
            }
    except Exception as e:
        logger.debug(f"jina_failed url={url}: {e}")

    # 2순위: Crawl4AI
    try:
        from app.services.crawl4ai_service import Crawl4AIService
        c4 = Crawl4AIService()
        if await c4.is_available():
            result = await asyncio.wait_for(
                c4.fetch_page(url, js_render=False),
                timeout=crawl_timeout,
            )
            if result and result.content and not result.error:
                # max_tokens 기준으로 truncate
                try:
                    from app.core.token_utils import CHARS_PER_TOKEN
                except Exception:
                    CHARS_PER_TOKEN = 2
                max_chars = max_tokens * CHARS_PER_TOKEN
                content = result.content[:max_chars]
                return {
                    "url": url,
                    "title": url,
                    "content": content,
                    "truncated": len(result.content) > max_chars,
                    "source": "crawl4ai",
                }
    except Exception as e:
        logger.debug(f"crawl4ai_failed url={url}: {e}")

    return None


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return url.strip()

    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith("utm_") or lower_key in _TRACKING_QUERY_KEYS:
            continue
        query_pairs.append((key, value))

    netloc = parts.netloc.lower()
    scheme = (parts.scheme or "https").lower()
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, urlencode(query_pairs, doseq=True), ""))


def _query_terms(query: str) -> List[str]:
    tokens = re.findall(r"[0-9A-Za-z가-힣]{2,}", query.lower())
    stopwords = {"about", "from", "into", "that", "this", "with", "what", "when", "where"}
    unique: List[str] = []
    for token in tokens:
        if token in stopwords:
            continue
        if token not in unique:
            unique.append(token)
    return unique[:10]


def _limit_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _extract_body_evidence(body: str, query_terms: List[str], max_chars: int = 600) -> str:
    if not body:
        return ""

    candidates: List[str] = []
    for chunk in re.split(r"(?:\n{2,}|[.!?]\s+)", body):
        normalized = re.sub(r"\s+", " ", chunk).strip(" -*#>\t\r\n")
        if len(normalized) < 30:
            continue
        lowered = normalized.lower()
        if any(term in lowered for term in query_terms):
            candidates.append(normalized)
        if len(candidates) >= 3:
            break

    if not candidates:
        for line in body.splitlines():
            normalized = re.sub(r"\s+", " ", line).strip(" -*#>\t\r\n")
            if len(normalized) >= 30:
                candidates.append(normalized)
                break

    evidence = " ... ".join(candidates)
    return _limit_text(evidence, max_chars)


def _match_score(
    *,
    query_terms: List[str],
    title: str,
    snippet: str,
    body: str,
    base_score: float,
) -> float:
    if not query_terms:
        return round(max(base_score, 0.0), 2)

    title_l = title.lower()
    snippet_l = snippet.lower()
    body_l = body.lower()
    denom = float(len(query_terms))

    title_hits = sum(1 for term in query_terms if term in title_l)
    snippet_hits = sum(1 for term in query_terms if term in snippet_l)
    body_hits = sum(1 for term in query_terms if term in body_l)

    weighted = (
        (title_hits / denom) * 25.0
        + (snippet_hits / denom) * 18.0
        + (body_hits / denom) * 47.0
        + max(base_score, 0.0) * 2.0
    )
    if body and body_hits:
        weighted += 5.0
    return round(weighted, 2)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _depth_to_complexity(depth: Any, query: str) -> str:
    if depth is None or depth == "":
        return detect_query_complexity(query)
    if isinstance(depth, int):
        if depth <= 0:
            return "SIMPLE"
        if depth == 1:
            return "MEDIUM"
        return "DEEP"

    text = str(depth).strip().lower()
    if text in {"0", "simple", "shallow", "light"}:
        return "SIMPLE"
    if text in {"2", "deep", "full"}:
        return "DEEP"
    return "MEDIUM"


def _fallback_synthesized_report(query: str, results: List[Dict[str, Any]]) -> str:
    if not results:
        return f"질문: {query}\n검색 결과가 부족해 종합 보고서를 만들지 못했습니다."

    lines = [f"질문: {query}", "", "핵심 근거:"]
    for idx, item in enumerate(results[:3], 1):
        evidence = item.get("body_evidence") or item.get("snippet") or "근거 부족"
        lines.append(
            f"{idx}. {item.get('title') or item.get('url')} "
            f"(score={item.get('match_score')}, source={item.get('source_attribution', {}).get('search_engine') or 'unknown'})"
        )
        lines.append(f"   {evidence}")
        lines.append(f"   {item.get('url')}")
    return "\n".join(lines)


async def _synthesize_match_report(
    query: str,
    results: List[Dict[str, Any]],
    synthesis_model: Optional[str] = None,
) -> Dict[str, Any]:
    if not results:
        return {"report": "", "model": synthesis_model or _MATCH_DEFAULT_SYNTHESIS_MODEL, "fallback_used": True}

    prompt_items = []
    for idx, item in enumerate(results[:5], 1):
        prompt_items.append(
            "\n".join(
                [
                    f"[{idx}] {item.get('title')}",
                    f"URL: {item.get('url')}",
                    f"match_score: {item.get('match_score')}",
                    f"search_engine: {item.get('source_attribution', {}).get('search_engine') or 'unknown'}",
                    f"crawl_source: {item.get('source_attribution', {}).get('crawl_source') or 'snippet_only'}",
                    f"snippet: {_limit_text(item.get('snippet', ''), 280)}",
                    f"body_evidence: {_limit_text(item.get('body_evidence', ''), 500)}",
                ]
            )
        )

    prompt = (
        "아래 검색/크롤링 근거만 사용해 CEO용 요약 보고서를 작성하세요.\n"
        f"질문: {query}\n\n"
        "형식:\n"
        "1. 핵심 결론 2~4문장\n"
        "2. 주요 근거 bullet 3개 이내, 각 bullet 끝에 [번호] 표기\n"
        "3. 불확실성/누락 1개\n\n"
        "근거:\n"
        f"{chr(10).join(prompt_items)}"
    )

    model = synthesis_model or _MATCH_DEFAULT_SYNTHESIS_MODEL
    try:
        report = await asyncio.wait_for(
            call_llm_with_fallback(
                prompt,
                model=model,
                max_tokens=700,
                system="검색 결과를 과장 없이 종합하고, 없는 내용은 추정하지 마세요.",
            ),
            timeout=_MATCH_SYNTHESIS_TIMEOUT,
        )
        if report:
            return {"report": report.strip(), "model": model, "fallback_used": False}
    except Exception as e:
        logger.warning("search_crawl_match_synthesis_failed: %s", str(e)[:160])

    return {"report": _fallback_synthesized_report(query, results), "model": model, "fallback_used": True}


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


async def search_crawl_match(
    query: str,
    *,
    max_results: int = 5,
    crawl_limit: int = 3,
    depth: Any = None,
    synthesis_model: Optional[str] = None,
    synthesize: bool = True,
) -> Dict[str, Any]:
    if not query or not query.strip():
        return {"error": "query 필수", "results": []}

    complexity = _depth_to_complexity(depth, query)
    max_results = max(1, min(int(max_results or 5), _MATCH_RESULT_LIMIT))
    crawl_limit_value = 3 if crawl_limit is None or crawl_limit == "" else int(crawl_limit)
    crawl_limit = max(0, min(crawl_limit_value, _MATCH_CRAWL_LIMIT))

    search_count_map = {"SIMPLE": max(max_results, 8), "MEDIUM": max(max_results * 3, 18), "DEEP": max(max_results * 4, 28)}
    crawl_limit_map = {
        "SIMPLE": crawl_limit,
        "MEDIUM": min(max(crawl_limit, 3), _MATCH_CRAWL_LIMIT),
        "DEEP": min(max(crawl_limit, 5), _MATCH_CRAWL_LIMIT),
    }
    effective_crawl_limit = min(crawl_limit_map.get(complexity, crawl_limit), _MATCH_CRAWL_LIMIT)
    if crawl_limit == 0:
        effective_crawl_limit = 0
    search_count = min(search_count_map.get(complexity, max_results * 3), 40)

    from app.services.searxng_search_service import search_searxng

    search_result = await search_searxng(query, categories="general", count=search_count)
    raw_results = search_result.get("results") or []
    if search_result.get("error") or not raw_results:
        return {
            "error": search_result.get("error", "검색 결과 없음"),
            "query": query,
            "results": [],
            "searched_count": 0,
            "crawled_count": 0,
            "failed_crawl_count": 0,
            "crawl_failures": [],
            "synthesized_report": "",
        }

    deduped_results: List[Dict[str, Any]] = []
    seen_urls = set()
    for item in raw_results:
        normalized_url = _normalize_url(str(item.get("url", "")))
        if not normalized_url or normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        copied = dict(item)
        copied["url"] = normalized_url
        deduped_results.append(copied)

    query_terms = _query_terms(query)
    crawl_map: Dict[str, Dict[str, Any]] = {}
    crawl_failures: List[Dict[str, str]] = []

    if effective_crawl_limit > 0:
        candidate_pool = sorted(
            deduped_results,
            key=lambda item: _safe_float(item.get("score")),
            reverse=True,
        )[: max(effective_crawl_limit * 3, effective_crawl_limit)]
        candidates_for_llm = [
            {
                "url": item["url"],
                "title": item.get("title", ""),
                "snippet": item.get("content", "")[:200],
            }
            for item in candidate_pool
        ]
        selected_urls = await _select_urls_by_llm(query, candidates_for_llm, effective_crawl_limit)
        crawl_raw = await asyncio.gather(
            *[
                _crawl_url_with_limits(url, max_tokens=_MATCH_BODY_MAX_TOKENS)
                for url in selected_urls
            ],
            return_exceptions=True,
        )
        for url, crawled in zip(selected_urls, crawl_raw):
            if isinstance(crawled, Exception):
                crawl_failures.append({"url": url, "error": str(crawled)[:160]})
                continue
            if not crawled or not crawled.get("content"):
                crawl_failures.append({"url": url, "error": "crawl_failed"})
                continue
            crawl_map[_normalize_url(crawled.get("url", url))] = crawled

    ranked_results: List[Dict[str, Any]] = []
    for idx, item in enumerate(deduped_results, 1):
        url = item.get("url", "")
        crawled = crawl_map.get(url)
        body = crawled.get("content", "") if crawled else ""
        snippet = item.get("content", "") or ""
        title = (crawled.get("title") if crawled else None) or item.get("title", "") or url
        body_evidence = _extract_body_evidence(body, query_terms) if body else ""
        match_score = _match_score(
            query_terms=query_terms,
            title=title,
            snippet=snippet,
            body=body,
            base_score=_safe_float(item.get("score")),
        )
        ranked_results.append(
            {
                "url": url,
                "title": title,
                "snippet": _limit_text(snippet, 320),
                "body": _limit_text(body, 1200) if body else "",
                "body_evidence": body_evidence,
                "match_score": match_score,
                "source_attribution": {
                    "search_engine": item.get("engine", ""),
                    "crawl_source": (crawled or {}).get("source", "snippet_only"),
                    "search_rank": idx,
                },
            }
        )

    ranked_results.sort(key=lambda item: item.get("match_score", 0.0), reverse=True)
    top_results = ranked_results[:max_results]

    synthesis = {"report": "", "model": synthesis_model or _MATCH_DEFAULT_SYNTHESIS_MODEL, "fallback_used": False}
    if synthesize:
        synthesis = await _synthesize_match_report(query, top_results, synthesis_model=synthesis_model)

    return {
        "query": query,
        "complexity": complexity,
        "searched_count": len(deduped_results),
        "crawled_count": len(crawl_map),
        "failed_crawl_count": len(crawl_failures),
        "crawl_failures": crawl_failures,
        "results": top_results,
        "synthesized_report": synthesis["report"],
        "synthesis_model": synthesis["model"],
        "synthesis_fallback_used": synthesis["fallback_used"],
    }
