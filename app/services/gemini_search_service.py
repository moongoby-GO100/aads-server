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
        Gemini 2.5 Flash + Google Search tool.
        google-genai SDK 우선, 없으면 REST API 폴백.
        """
        if not self._api_key:
            raise ValueError("GEMINI_API_KEY not set")

        try:
            from google import genai as genai_sdk
            from google.genai import types as genai_types

            client = genai_sdk.Client(api_key=self._api_key)
            prompt = f"{context}\n\n{query}" if context else query

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                ),
            )

            text = response.text or ""
            citations: List[Dict[str, Any]] = []
            queries: List[str] = []

            if response.candidates:
                candidate = response.candidates[0]
                gm = getattr(candidate, "grounding_metadata", None)
                if gm:
                    if hasattr(gm, "web_search_queries"):
                        queries = list(gm.web_search_queries or [])
                    if hasattr(gm, "grounding_chunks"):
                        for chunk in (gm.grounding_chunks or [])[:5]:
                            web = getattr(chunk, "web", None)
                            if web:
                                citations.append({
                                    "url": getattr(web, "uri", "") or "",
                                    "title": getattr(web, "title", "") or "",
                                    "favicon": f"https://www.google.com/s2/favicons?domain={getattr(web, 'uri', '') or ''}",
                                })

            return SearchResult(text=text, citations=citations, queries=queries)

        except ImportError:
            return await self._search_via_rest(query, context)
        except Exception as e:
            logger.warning(f"gemini_search_sdk_error (fallback to REST): {e}")
            return await self._search_via_rest(query, context)

    async def _search_via_rest(self, query: str, context: str = "") -> SearchResult:
        """REST API 폴백 — google_search tool (2.5 호환)."""
        import httpx
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={self._api_key}"
        )
        prompt = f"{context}\n\n{query}" if context else query
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
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
