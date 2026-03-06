"""
AADS-130: MCP Client 서비스 모듈 — Brave Search, Fetch MCP 래퍼.
app.mcp.client 를 서비스 계층에서 사용하기 위한 인터페이스 모듈.

사용법:
    from app.services.mcp_client import brave_search, fetch_url
    results = await brave_search("AI SaaS 시장 2025")
    content = await fetch_url("https://example.com")
"""
from __future__ import annotations

import structlog
from typing import Optional

logger = structlog.get_logger()


async def brave_search(query: str, count: int = 5) -> list[dict]:
    """Brave Search MCP를 통한 웹 검색.

    Args:
        query: 검색 쿼리
        count: 결과 수 (기본 5)

    Returns:
        검색 결과 리스트 [{"title": ..., "url": ..., "snippet": ...}]
    """
    try:
        from app.mcp.client import get_mcp_manager
        manager = get_mcp_manager()
        if manager is None:
            logger.warning("mcp_manager_not_initialized")
            return []

        tools = await manager.get_tools()
        search_tool = next(
            (t for t in tools if "brave_web_search" in t.name.lower()),
            None,
        )
        if search_tool is None:
            logger.warning("brave_search_tool_not_found")
            return []

        result = await search_tool.ainvoke({"query": query, "count": count})
        if isinstance(result, list):
            return result
        return [{"raw": str(result)}]
    except Exception as e:
        logger.error("brave_search_error", query=query, error=str(e))
        return []


async def fetch_url(url: str) -> str:
    """Fetch MCP를 통한 URL 콘텐츠 가져오기.

    Args:
        url: 가져올 URL

    Returns:
        페이지 텍스트 콘텐츠
    """
    try:
        from app.mcp.client import get_mcp_manager
        manager = get_mcp_manager()
        if manager is None:
            logger.warning("mcp_manager_not_initialized")
            return ""

        tools = await manager.get_tools()
        fetch_tool = next(
            (t for t in tools if "fetch" in t.name.lower()),
            None,
        )
        if fetch_tool is None:
            logger.warning("fetch_tool_not_found")
            return ""

        result = await fetch_tool.ainvoke({"url": url})
        return str(result)
    except Exception as e:
        logger.error("fetch_url_error", url=url, error=str(e))
        return ""


__all__ = ["brave_search", "fetch_url"]
