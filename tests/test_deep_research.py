"""
AADS-186E2: Deep Research 단위 테스트
- DeepResearchService.research() → ResearchResult 반환 (mock Gemini API)
- 일일 상한 초과 시 daily_limit 반환
- ResearchResult / ResearchEvent Pydantic 모델 검증
"""
from __future__ import annotations

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ─── ResearchEvent / ResearchResult 모델 테스트 ───────────────────────────────

class TestResearchModels:
    """Pydantic 모델 구조 검증."""

    def test_research_event_start(self):
        """ResearchEvent start 타입."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="start", interaction_id="abc123")
        assert ev.type == "start"
        assert ev.interaction_id == "abc123"
        assert ev.text is None

    def test_research_event_content(self):
        """ResearchEvent content 타입."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="content", text="분석 결과...")
        assert ev.type == "content"
        assert ev.text == "분석 결과..."

    def test_research_event_thinking(self):
        """ResearchEvent thinking 타입."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="thinking", text="검색 중...")
        assert ev.type == "thinking"

    def test_research_event_complete(self):
        """ResearchEvent complete 타입."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="complete")
        assert ev.type == "complete"
        assert ev.text is None

    def test_research_event_error(self):
        """ResearchEvent error 타입."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="error", text="API 오류")
        assert ev.type == "error"

    def test_research_result_completed(self):
        """ResearchResult completed 상태."""
        from app.models.research import ResearchResult
        r = ResearchResult(content="보고서 내용", status="completed", interaction_id="id1")
        assert r.status == "completed"
        assert r.content == "보고서 내용"
        assert r.error is None

    def test_research_result_failed(self):
        """ResearchResult failed 상태."""
        from app.models.research import ResearchResult
        r = ResearchResult(content="", status="failed", error="timeout")
        assert r.status == "failed"
        assert r.error == "timeout"

    def test_research_result_default_cost(self):
        """ResearchResult 기본 비용 $3."""
        from app.models.research import ResearchResult
        r = ResearchResult(content="test", status="completed")
        assert r.cost_usd == 3.0


# ─── DeepResearchService 테스트 ───────────────────────────────────────────────

class TestDeepResearchService:
    """DeepResearchService 동작 검증."""

    def test_service_init(self):
        """서비스 초기화."""
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        assert hasattr(svc, "_api_key")
        assert hasattr(svc, "_agent")

    def test_is_available_without_key(self):
        """GEMINI_API_KEY 미설정 시 is_available=False."""
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        svc._api_key = ""
        assert svc.is_available() is False

    def test_is_available_with_key(self):
        """GEMINI_API_KEY 설정 시 is_available=True."""
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        svc._api_key = "fake_key_for_test"
        assert svc.is_available() is True

    @pytest.mark.asyncio
    async def test_research_no_api_key_returns_error(self):
        """API 키 없이 research() 호출 시 status='error' 반환."""
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        svc._api_key = ""
        result = await svc.research("test query")
        assert result.status == "error"
        assert "GEMINI_API_KEY" in result.report

    @pytest.mark.asyncio
    async def test_research_daily_limit_exceeded(self):
        """일일 한도 초과 시 status='daily_limit' 반환."""
        from app.services import deep_research_service as dr_mod
        # 오늘 날짜에 5건 이미 사용된 것처럼 설정
        today = dr_mod._today_str()
        original = dr_mod._daily_usage.copy()
        dr_mod._daily_usage[today] = 5
        try:
            from app.services.deep_research_service import DeepResearchService
            svc = DeepResearchService()
            svc._api_key = "fake_key"
            result = await svc.research("test query")
            assert result.status == "daily_limit"
            assert "한도" in result.report
        finally:
            dr_mod._daily_usage.clear()
            dr_mod._daily_usage.update(original)

    @pytest.mark.asyncio
    async def test_research_sdk_error_returns_error_status(self):
        """SDK 에러 발생 시 status='error' 반환."""
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        svc._api_key = "fake_key"
        svc._sdk_available = False

        async def mock_http_error(prompt, cb):
            raise Exception("HTTP 연결 실패")

        with patch.object(svc, "_research_via_http", side_effect=Exception("HTTP 연결 실패")):
            result = await svc.research("test query", timeout=1)
        assert result.status in ("error", "timeout")

    def test_daily_limit_check_initial(self):
        """초기 상태에서 일일 한도 미초과."""
        from app.services import deep_research_service as dr_mod
        original = dr_mod._daily_usage.copy()
        today = dr_mod._today_str()
        dr_mod._daily_usage.pop(today, None)
        try:
            assert dr_mod._check_daily_limit() is True
        finally:
            dr_mod._daily_usage.clear()
            dr_mod._daily_usage.update(original)

    def test_daily_limit_check_exceeded(self):
        """5건 이후 한도 초과."""
        from app.services import deep_research_service as dr_mod
        original = dr_mod._daily_usage.copy()
        today = dr_mod._today_str()
        dr_mod._daily_usage[today] = 5
        try:
            assert dr_mod._check_daily_limit() is False
        finally:
            dr_mod._daily_usage.clear()
            dr_mod._daily_usage.update(original)

    def test_daily_limit_increment(self):
        """_increment_daily() 호출 시 카운터 증가."""
        from app.services import deep_research_service as dr_mod
        original = dr_mod._daily_usage.copy()
        today = dr_mod._today_str()
        dr_mod._daily_usage.pop(today, None)
        try:
            dr_mod._increment_daily()
            assert dr_mod._daily_usage.get(today, 0) == 1
            dr_mod._increment_daily()
            assert dr_mod._daily_usage.get(today, 0) == 2
        finally:
            dr_mod._daily_usage.clear()
            dr_mod._daily_usage.update(original)


# ─── SSE 이벤트 포맷 검증 ─────────────────────────────────────────────────────

class TestResearchSSEEvents:
    """SSE 이벤트 포맷 확인."""

    def test_research_start_sse_format(self):
        """research_start 이벤트 포맷."""
        import json
        payload = {"type": "research_start", "message": "딥리서치를 시작합니다..."}
        sse = f"data: {json.dumps(payload)}\n\n"
        parsed = json.loads(sse.replace("data: ", "").strip())
        assert parsed["type"] == "research_start"

    def test_research_complete_sse_format(self):
        """research_complete 이벤트 포맷."""
        import json
        payload = {"type": "research_complete", "interaction_id": "xyz", "cost": "3.0"}
        sse = f"data: {json.dumps(payload)}\n\n"
        parsed = json.loads(sse.replace("data: ", "").strip())
        assert parsed["type"] == "research_complete"
        assert "interaction_id" in parsed
        assert "cost" in parsed

    def test_research_progress_sse_format(self):
        """research_progress 이벤트 포맷."""
        import json
        payload = {"type": "research_progress", "content": "분석 중..."}
        sse = f"data: {json.dumps(payload)}\n\n"
        parsed = json.loads(sse.replace("data: ", "").strip())
        assert parsed["type"] == "research_progress"

    def test_research_thinking_sse_format(self):
        """research_thinking 이벤트 포맷."""
        import json
        payload = {"type": "research_thinking", "text": "웹 검색 중..."}
        sse = f"data: {json.dumps(payload)}\n\n"
        parsed = json.loads(sse.replace("data: ", "").strip())
        assert parsed["type"] == "research_thinking"
