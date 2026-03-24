"""
SearXNG 메타검색 서비스.
- Google/Bing/DuckDuckGo/Brave 등 70개+ 엔진 동시 검색 (무료, 무제한)
- 내부 Docker 네트워크로 aads-searxng:8080 호출
- httpx AsyncClient, 타임아웃 10초
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

SEARXNG_BASE_URL = os.getenv("SEARXNG_BASE_URL", "http://aads-searxng:8080")
_TIMEOUT = 10.0


async def search_searxng(
    query: str,
    categories: str = "general",
    language: str = "ko-KR",
    time_range: Optional[str] = None,
    engines: Optional[str] = None,
    count: int = 10,
) -> Dict[str, Any]:
    """
    SearXNG JSON API 검색.

    Args:
        query: 검색 쿼리 (필수)
        categories: general, images, news, videos, it, science, files, music
        language: ko-KR, en-US 등
        time_range: day, week, month, year (선택)
        engines: 특정 엔진 지정 (콤마 구분, 선택)
        count: 결과 개수 (기본 10)

    Returns:
        {results: [...], query: str, number_of_results: int, engines_used: [...]}
    """
    if not query:
        return {"error": "query 필수", "results": []}

    params: Dict[str, Any] = {
        "q": query,
        "format": "json",
        "categories": categories,
        "language": language,
        "pageno": 1,
    }
    if time_range:
        params["time_range"] = time_range
    if engines:
        params["engines"] = engines

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{SEARXNG_BASE_URL}/search", params=params)
            resp.raise_for_status()
            data = resp.json()

        raw_results: List[Dict[str, Any]] = data.get("results", [])
        # 결과 파싱 및 정리
        parsed: List[Dict[str, str]] = []
        for r in raw_results[:count]:
            parsed.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "engine": ", ".join(r.get("engines", [])),
                "score": str(r.get("score", 0)),
            })

        engines_used = list({
            e for r in raw_results for e in r.get("engines", [])
        })

        return {
            "results": parsed,
            "query": query,
            "number_of_results": data.get("number_of_results", len(parsed)),
            "engines_used": sorted(engines_used),
        }

    except httpx.TimeoutException:
        logger.warning("searxng_timeout", extra={"query": query})
        return {"error": "SearXNG 타임아웃", "results": [], "query": query}
    except httpx.HTTPStatusError as e:
        logger.warning("searxng_http_error", extra={"query": query, "status": e.response.status_code})
        return {"error": f"SearXNG HTTP {e.response.status_code}", "results": [], "query": query}
    except Exception as e:
        logger.error("searxng_error", extra={"query": query, "error": str(e)})
        return {"error": f"SearXNG 연결 실패: {str(e)}", "results": [], "query": query}
