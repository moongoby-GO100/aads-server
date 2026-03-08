"""
AADS-186E-1: 크롤링 도구 테스트
- jina_read: URL → 마크다운 변환
- deep_crawl: 검색 → 크롤링 → 요약 파이프라인
- Jina 실패 시 crawl4ai 폴백 (mock)
- max_tokens 초과 시 truncation
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── JinaReaderService ───────────────────────────────────────────────────────

class TestJinaReaderService:
    @pytest.mark.asyncio
    async def test_read_url_success(self):
        """jina_read: URL → 마크다운 반환 확인."""
        from app.services.jina_reader_service import JinaReaderService, JinaResult

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "# Example Domain\n\nThis is a test page content."

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            svc = JinaReaderService()
            result = await svc.read_url("https://example.com")

        assert result is not None
        assert isinstance(result, JinaResult)
        assert "Example Domain" in result.title
        assert "test page content" in result.content
        assert result.word_count > 0
        assert result.source_url == "https://example.com"
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_read_url_truncation(self):
        """max_tokens 초과 시 '[내용 절삭됨]' 표시 확인."""
        from app.services.jina_reader_service import JinaReaderService

        # 1토큰=4자 기준, max_tokens=10 → max_chars=40
        long_content = "A" * 200  # 40자 초과
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = long_content

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            svc = JinaReaderService()
            result = await svc.read_url("https://example.com", max_tokens=10)

        assert result is not None
        assert result.truncated is True
        assert "[내용 절삭됨]" in result.content

    @pytest.mark.asyncio
    async def test_read_url_failure_returns_none(self):
        """Jina 실패(HTTP 5xx) 시 None 반환 확인."""
        from app.services.jina_reader_service import JinaReaderService

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            svc = JinaReaderService()
            result = await svc.read_url("https://example.com", timeout=5)

        assert result is None


# ─── Crawl4AIService ─────────────────────────────────────────────────────────

class TestCrawl4AIService:
    @pytest.mark.asyncio
    async def test_fetch_page_unavailable(self):
        """Crawl4AI 미설치 시 None 반환 (graceful skip) 확인."""
        from app.services.crawl4ai_service import Crawl4AIService

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client_cls.return_value = mock_client

            svc = Crawl4AIService()
            result = await svc.fetch_page("https://example.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_page_success(self):
        """Crawl4AI 가용 시 정상 크롤링 확인."""
        from app.services.crawl4ai_service import Crawl4AIService, CrawlResult

        health_response = MagicMock()
        health_response.status_code = 200

        crawl_response = MagicMock()
        crawl_response.status_code = 200
        crawl_response.json.return_value = {
            "results": [{"markdown": "# Test Page\n\nSome content here."}]
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=health_response)
            mock_client.post = AsyncMock(return_value=crawl_response)
            mock_client_cls.return_value = mock_client

            svc = Crawl4AIService()
            result = await svc.fetch_page("https://example.com")

        assert result is not None
        assert isinstance(result, CrawlResult)
        assert "Test Page" in result.content
        assert result.word_count > 0


# ─── ToolExecutor — jina_read 폴백 ──────────────────────────────────────────

class TestJinaReadFallback:
    @pytest.mark.asyncio
    async def test_jina_read_falls_back_to_crawl4ai(self):
        """Jina 실패 시 crawl4ai 폴백 확인 (mock)."""
        from app.services.tool_executor import ToolExecutor
        from app.services.crawl4ai_service import CrawlResult

        crawl4ai_result = CrawlResult(
            url="https://example.com",
            content="# Fallback content from crawl4ai",
            word_count=5,
            js_rendered=True,
        )

        with patch("app.services.jina_reader_service.JinaReaderService.read_url", new_callable=AsyncMock) as mock_jina, \
             patch("app.services.crawl4ai_service.Crawl4AIService.fetch_page", new_callable=AsyncMock) as mock_c4:
            mock_jina.return_value = None  # Jina 실패
            mock_c4.return_value = crawl4ai_result

            executor = ToolExecutor()
            result_str = await executor.execute("jina_read", {"url": "https://example.com"})

        import json
        result = json.loads(result_str)
        assert "Fallback content" in result.get("content", "")
        assert result.get("via") == "crawl4ai_fallback"


# ─── DeepCrawlService ────────────────────────────────────────────────────────

class TestDeepCrawlService:
    @pytest.mark.asyncio
    async def test_research_crawl_with_citations(self):
        """deep_crawl: 검색 → 크롤링 → 인용 포함 종합 결과 확인."""
        from app.services.deep_crawl_service import DeepCrawlService
        from app.services.jina_reader_service import JinaResult

        jina_result = JinaResult(
            title="FastAPI Health Check Guide",
            content="# FastAPI Health Check\n\nHealthcheck implementation for FastAPI applications.",
            word_count=10,
            source_url="https://fastapi.tiangolo.com/health",
        )

        with patch.object(DeepCrawlService, "_search", new_callable=AsyncMock) as mock_search, \
             patch("app.services.jina_reader_service.JinaReaderService.read_url", new_callable=AsyncMock) as mock_jina, \
             patch.object(DeepCrawlService, "_llm_call", new_callable=AsyncMock) as mock_llm:

            mock_search.return_value = ["https://fastapi.tiangolo.com/health"]
            mock_jina.return_value = jina_result
            mock_llm.return_value = "FastAPI 헬스체크 종합 분석: 표준 /health 엔드포인트 구현 권장."

            svc = DeepCrawlService()
            result = await svc.research_crawl("FastAPI health check", max_pages=1, summarize=True)

        assert result.query == "FastAPI health check"
        assert len(result.citations) == 1
        assert result.pages_crawled == 1
        assert "synthesis" in dir(result)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_research_crawl_no_results(self):
        """검색 결과 없을 때 에러 반환 확인."""
        from app.services.deep_crawl_service import DeepCrawlService

        with patch.object(DeepCrawlService, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = []

            svc = DeepCrawlService()
            result = await svc.research_crawl("xyzzy_no_results_query")

        assert result.error == "no_search_results"
        assert result.pages_crawled == 0
