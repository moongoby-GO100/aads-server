"""
AADS-185: Gemini Google Search Grounding 서비스
google-generativeai SDK 사용 (기존 langchain-google-genai 패키지 경유)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


@dataclass
class SearchResult:
    text: str
    citations: List[Dict[str, Any]] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    error: Optional[str] = None


class GeminiSearchService:
    """
    Gemini 2.5 Flash + Google Search Grounding.
    langchain-google-genai 패키지를 통해 호출.
    """

    def __init__(self) -> None:
        self._api_key = GEMINI_API_KEY

    async def search_grounded(self, query: str, context: str = "") -> SearchResult:
        """
        Gemini Flash + Google Search Grounding.
        groundingMetadata → CitationCard 데이터로 변환.
        """
        if not self._api_key:
            raise ValueError("GEMINI_API_KEY not set")

        try:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=self._api_key)

            model = genai.GenerativeModel(
                model_name="gemini-2.5-flash",
                tools="google_search_retrieval",
            )

            prompt = f"{context}\n\n{query}" if context else query
            response = model.generate_content(prompt)

            text = response.text or ""
            citations: List[Dict[str, Any]] = []
            queries: List[str] = []

            # groundingMetadata 파싱
            if hasattr(response, "candidates") and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, "grounding_metadata") and candidate.grounding_metadata:
                    gm = candidate.grounding_metadata
                    # 검색 쿼리
                    if hasattr(gm, "web_search_queries"):
                        queries = list(gm.web_search_queries or [])
                    # 출처 청크
                    if hasattr(gm, "grounding_chunks"):
                        for chunk in (gm.grounding_chunks or [])[:5]:
                            if hasattr(chunk, "web"):
                                citations.append({
                                    "url": chunk.web.uri or "",
                                    "title": chunk.web.title or "",
                                    "favicon": f"https://www.google.com/s2/favicons?domain={chunk.web.uri or ''}",
                                })

            return SearchResult(text=text, citations=citations, queries=queries)

        except ImportError:
            # google-generativeai 미설치 시 httpx 직접 호출
            return await self._search_via_api(query)
        except Exception as e:
            logger.error(f"gemini_search_service error: {e}")
            raise

    async def _search_via_api(self, query: str) -> SearchResult:
        """google-generativeai 미설치 시 REST API 직접 호출."""
        import httpx
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={self._api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": query}]}],
            "tools": [{"googleSearchRetrieval": {}}],
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, json=payload)
            if r.status_code != 200:
                raise ValueError(f"Gemini API error {r.status_code}: {r.text[:300]}")
            data = r.json()

        text = ""
        citations = []
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            gm = candidates[0].get("groundingMetadata", {})
            for chunk in gm.get("groundingChunks", [])[:5]:
                web = chunk.get("web", {})
                if web:
                    citations.append({
                        "url": web.get("uri", ""),
                        "title": web.get("title", ""),
                        "favicon": f"https://www.google.com/s2/favicons?domain={web.get('uri', '')}",
                    })

        return SearchResult(text=text, citations=citations)
