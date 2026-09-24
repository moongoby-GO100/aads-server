"""
AADS-185: 검색 그라운딩 서비스.

2026-09-24(CEO 지시): Gemini Grounding 제거. Claude(Anthropic) 네이티브
web_search 도구를 1순위로, OpenAI 네이티브 web_search 도구를 2순위(타사 동급)로
사용한다. 클래스/데이터클래스 이름은 기존 4개 호출부(tool_executor.py,
ceo_chat_tools.py, chat_service.py)와의 호환을 위해 유지한다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    text: str
    citations: List[Dict[str, Any]] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    error: Optional[str] = None


class GeminiSearchService:
    """
    Claude 네이티브 web_search 도구(claude-haiku-4-5, 저비용) 1순위,
    OpenAI 네이티브 web_search 도구(gpt-5-mini) 2순위. Gemini는 사용하지 않는다.
    """

    def __init__(self) -> None:
        self._api_key = "claude+openai_web_search"  # 하위 호환용 truthy placeholder

    async def search_grounded(self, query: str, context: str = "") -> SearchResult:
        prompt = f"{context}\n\n{query}" if context else query
        try:
            return await self._search_claude(prompt)
        except Exception as e:
            logger.warning(f"claude_web_search_failed (falling back to OpenAI): {e}")
        try:
            return await self._search_openai(prompt)
        except Exception as e:
            logger.warning(f"openai_web_search_failed (falling back to SearXNG): {e}")
        return await self._search_via_rest(query, context)

    async def _search_claude(self, prompt: str) -> SearchResult:
        from app.core.auth_provider import create_anthropic_client

        client = create_anthropic_client()
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": prompt}],
        )

        text_parts: List[str] = []
        citations: List[Dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "text":
                continue
            text_parts.append(block.text)
            for cite in (getattr(block, "citations", None) or []):
                url = getattr(cite, "url", None)
                if url:
                    citations.append({
                        "url": url,
                        "title": getattr(cite, "title", "") or "",
                        "favicon": f"https://www.google.com/s2/favicons?domain={url}",
                    })

        text = "".join(text_parts)
        if not text:
            raise ValueError("Claude web_search returned no text content")
        return SearchResult(text=text, citations=citations)

    async def _search_openai(self, prompt: str) -> SearchResult:
        from openai import AsyncOpenAI

        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set")
        client = AsyncOpenAI(api_key=api_key)
        response = await client.responses.create(
            model="gpt-5-mini",
            tools=[{"type": "web_search_preview"}],
            input=prompt,
        )

        text = getattr(response, "output_text", "") or ""
        citations: List[Dict[str, Any]] = []
        for item in (getattr(response, "output", None) or []):
            for content in (getattr(item, "content", None) or []):
                for ann in (getattr(content, "annotations", None) or []):
                    url = getattr(ann, "url", None)
                    if url:
                        citations.append({
                            "url": url,
                            "title": getattr(ann, "title", "") or "",
                            "favicon": f"https://www.google.com/s2/favicons?domain={url}",
                        })

        if not text:
            raise ValueError("OpenAI web_search returned no text content")
        return SearchResult(text=text, citations=citations)

    async def _search_via_rest(self, query: str, context: str = "") -> SearchResult:
        """3순위 최종 폴백. Claude/OpenAI 네이티브 도구가 모두 실패했을 때만 탄다.

        REST 호출로 외부 검색 API를 직접 때리는 대신, 이미 검증된 내부
        SearXNG 메타검색(aads-searxng, 70개+ 엔진, 무료)을 재사용한다.
        """
        from app.services.searxng_search_service import search_searxng

        data = await search_searxng(f"{context} {query}".strip() if context else query)
        if data.get("error"):
            return SearchResult(text="", error=data["error"])

        results = data.get("results", [])
        text = "\n\n".join(f"{r.get('title', '')}\n{r.get('content', '')}" for r in results[:5])
        citations = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "favicon": f"https://www.google.com/s2/favicons?domain={r.get('url', '')}",
            }
            for r in results[:5] if r.get("url")
        ]
        return SearchResult(text=text, citations=citations)
