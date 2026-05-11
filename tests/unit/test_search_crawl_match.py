from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_search_crawl_match_body_reranks_results():
    from app.services.smart_search_service import search_crawl_match

    search_payload = {
        "results": [
            {
                "title": "General platform update",
                "url": "https://example.com/general?utm_source=newsletter#top",
                "content": "Weekly company update with broad platform notes.",
                "engine": "google",
                "score": "9.5",
            },
            {
                "title": "Health check implementation guide",
                "url": "https://example.com/fastapi-health",
                "content": "Quick overview only.",
                "engine": "bing",
                "score": "1.2",
            },
        ]
    }

    crawl_side_effect = [
        {
            "url": "https://example.com/general",
            "title": "General platform update",
            "content": "Release notes and hiring updates without any API health guidance.",
            "source": "jina",
        },
        {
            "url": "https://example.com/fastapi-health",
            "title": "Health check implementation guide",
            "content": (
                "FastAPI health check endpoints should expose liveness and readiness probes. "
                "A health check route can also validate downstream dependencies."
            ),
            "source": "jina",
        },
    ]

    with patch(
        "app.services.searxng_search_service.search_searxng",
        new=AsyncMock(return_value=search_payload),
    ), patch(
        "app.services.smart_search_service._crawl_url_with_limits",
        new=AsyncMock(side_effect=crawl_side_effect),
    ):
        result = await search_crawl_match(
            "FastAPI health check",
            max_results=2,
            crawl_limit=2,
            synthesize=False,
        )

    assert result["results"][0]["url"] == "https://example.com/fastapi-health"
    assert result["results"][0]["match_score"] > result["results"][1]["match_score"]
    assert "health check" in result["results"][0]["body_evidence"].lower()
    assert result["results"][0]["source_attribution"]["crawl_source"] == "jina"
    assert result["results"][1]["url"] == "https://example.com/general"


@pytest.mark.asyncio
async def test_search_crawl_match_tolerates_partial_crawl_failure():
    from app.services.smart_search_service import search_crawl_match

    search_payload = {
        "results": [
            {
                "title": "Primary source",
                "url": "https://example.com/primary",
                "content": "Primary source snippet",
                "engine": "google",
                "score": "4.2",
            },
            {
                "title": "Secondary source",
                "url": "https://example.com/secondary",
                "content": "Secondary source snippet",
                "engine": "duckduckgo",
                "score": "3.8",
            },
        ]
    }

    with patch(
        "app.services.searxng_search_service.search_searxng",
        new=AsyncMock(return_value=search_payload),
    ), patch(
        "app.services.smart_search_service._crawl_url_with_limits",
        new=AsyncMock(
            side_effect=[
                None,
                {
                    "url": "https://example.com/secondary",
                    "title": "Secondary source",
                    "content": "Secondary evidence with detailed dependency notes for the query.",
                    "source": "crawl4ai",
                },
            ]
        ),
    ):
        result = await search_crawl_match(
            "dependency notes",
            max_results=2,
            crawl_limit=2,
            synthesize=False,
        )

    assert result["failed_crawl_count"] == 1
    assert result["crawled_count"] == 1
    assert len(result["crawl_failures"]) == 1
    assert len(result["results"]) == 2
    assert any(item["url"] == "https://example.com/primary" for item in result["results"])
    assert any(
        item["source_attribution"]["crawl_source"] == "crawl4ai"
        for item in result["results"]
    )


@pytest.mark.asyncio
async def test_tool_executor_search_crawl_match_dispatch():
    from app.services.tool_executor import ToolExecutor

    mocked = AsyncMock(
        return_value={
            "query": "fastapi",
            "results": [],
            "synthesized_report": "",
        }
    )
    with patch(
        "app.services.smart_search_service.search_crawl_match",
        new=mocked,
    ):
        raw = await ToolExecutor().execute(
            "search_crawl_match",
            {"query": "fastapi", "_selected_model": "gpt-5.5", "synthesize": False},
        )

    payload = json.loads(raw)
    assert payload["query"] == "fastapi"
    assert payload["results"] == []
    assert mocked.await_args.kwargs["synthesis_model"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_search_crawl_match_fallback_model_is_gpt55():
    from app.services.smart_search_service import _synthesize_match_report

    synthesis = await _synthesize_match_report("query", [], synthesis_model=None)

    assert synthesis["model"] == "gpt-5.5"
