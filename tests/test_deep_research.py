"""
AADS-186E2: Deep Research 단위 테스트
- DeepResearchService 초기화 및 가용성 확인
- 일일 상한 초과 시 거부 메시지 확인
- ResearchEvent / ResearchResult Pydantic 모델 검증
- deep_research 도구 등록 확인 (tool_registry)
- intent_router deep_research 인텐트 키워드 인식 확인
"""
from __future__ import annotations

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ─── ResearchEvent / ResearchResult 모델 ─────────────────────────────────────

class TestResearchModels:
    """Pydantic 모델 기본 검증."""

    def test_research_event_start(self):
        """start 이벤트 생성."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="start", interaction_id="test-id-123")
        assert ev.type == "start"
        assert ev.interaction_id == "test-id-123"
        assert ev.text is None

    def test_research_event_content(self):
        """content 이벤트 생성."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="content", text="시장 분석 결과...")
        assert ev.type == "content"
        assert ev.text == "시장 분석 결과..."

    def test_research_event_thinking(self):
        """thinking 이벤트 생성."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="thinking", text="검색 중: AI 에이전트 시장...")
        assert ev.type == "thinking"
        assert ev.text is not None

    def test_research_event_complete(self):
        """complete 이벤트 생성."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="complete")
        assert ev.type == "complete"

    def test_research_event_error(self):
        """error 이벤트 생성."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="error", text="API 오류")
        assert ev.type == "error"

    def test_research_result_completed(self):
        """completed 결과 모델."""
        from app.models.research import ResearchResult
        r = ResearchResult(
            content="AI 에이전트 시장은 연 40% 성장...",
            interaction_id="abc-123",
            status="completed",
        )
        assert r.status == "completed"
        assert "AI" in r.content
        assert r.error is None

    def test_research_result_failed(self):
        """failed 결과 모델."""
        from app.models.research import ResearchResult
        r = ResearchResult(
            content="",
            interaction_id="xyz-999",
            status="failed",
            error="API timeout",
        )
        assert r.status == "failed"
        assert r.error == "API timeout"

    def test_research_result_daily_limit(self):
        """daily_limit 상태 모델."""
        from app.models.research import ResearchResult
        r = ResearchResult(
            content="[일일 상한 초과]",
            interaction_id="",
            status="daily_limit",
        )
        assert r.status == "daily_limit"


# ─── DeepResearchService 초기화 테스트 ───────────────────────────────────────

class TestDeepResearchServiceInit:
    """DeepResearchService 클래스 기본 동작."""

    def test_service_exists(self):
        """DeepResearchService 임포트 가능."""
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        assert svc is not None

    def test_is_available_no_api_key(self):
        """GEMINI_API_KEY 미설정 시 is_available() → False."""
        from app.services.deep_research_service import DeepResearchService
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            svc = DeepResearchService()
            svc._api_key = ""
            assert svc.is_available() is False

    def test_is_available_with_api_key(self):
        """GEMINI_API_KEY 설정 시 is_available() → True."""
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        svc._api_key = "test-api-key"
        assert svc.is_available() is True

    def test_daily_limit_constant(self):
        """일일 상한 = 5건."""
        from app.services import deep_research_service as m
        assert m._DAILY_LIMIT == 5

    def test_timeout_constants(self):
        """타임아웃 상수: 표준 20분, 복잡 60분."""
        from app.services import deep_research_service as m
        assert m._TIMEOUT_STANDARD == 1200  # 20분
        assert m._TIMEOUT_COMPLEX == 3600   # 60분


# ─── 일일 상한 초과 테스트 ────────────────────────────────────────────────────

class TestDailyLimit:
    """일일 5건 상한 동작 검증."""

    def setup_method(self):
        """각 테스트 전 카운터 초기화."""
        from app.services import deep_research_service as m
        m._daily_usage.clear()

    def test_daily_limit_not_exceeded(self):
        """0건 → 상한 미초과."""
        from app.services.deep_research_service import _check_daily_limit
        assert _check_daily_limit() is True

    def test_daily_limit_at_4(self):
        """4건 → 상한 미초과."""
        from app.services import deep_research_service as m
        from app.services.deep_research_service import _check_daily_limit, _today_str
        m._daily_usage[_today_str()] = 4
        assert _check_daily_limit() is True

    def test_daily_limit_at_5(self):
        """5건 → 상한 초과."""
        from app.services import deep_research_service as m
        from app.services.deep_research_service import _check_daily_limit, _today_str
        m._daily_usage[_today_str()] = 5
        assert _check_daily_limit() is False

    def test_daily_limit_at_10(self):
        """10건 → 상한 초과."""
        from app.services import deep_research_service as m
        from app.services.deep_research_service import _check_daily_limit, _today_str
        m._daily_usage[_today_str()] = 10
        assert _check_daily_limit() is False

    @pytest.mark.asyncio
    async def test_research_rejects_on_daily_limit(self):
        """일일 상한 초과 시 research() → status='daily_limit'."""
        from app.services import deep_research_service as m
        from app.services.deep_research_service import DeepResearchService, _today_str
        # 상한 강제 설정
        m._daily_usage[_today_str()] = 5
        svc = DeepResearchService()
        svc._api_key = "fake-key"
        result = await svc.research("AI 시장 조사")
        assert result.status == "daily_limit"
        assert "한도" in result.report or "daily_limit" in result.status


# ─── API 키 없을 때 graceful 처리 ─────────────────────────────────────────────

class TestNoApiKeyGraceful:
    """GEMINI_API_KEY 미설정 시 graceful 비활성화."""

    @pytest.mark.asyncio
    async def test_research_no_api_key(self):
        """API 키 없이 research() → status='error', 오류 메시지 반환."""
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        svc._api_key = ""  # 키 제거
        result = await svc.research("테스트 쿼리")
        assert result.status == "error"
        assert result.report != ""  # 오류 메시지 포함


# ─── tool_registry deep_research 등록 테스트 ─────────────────────────────────

class TestDeepResearchToolRegistry:
    """tool_registry에 deep_research 도구 등록 확인."""

    def test_deep_research_in_registry(self):
        """deep_research 도구 존재."""
        from app.services import tool_registry as tr
        assert "deep_research" in tr._TOOLS

    def test_deep_research_has_query_field(self):
        """deep_research 스키마에 query 필드 포함."""
        from app.services import tool_registry as tr
        schema = tr._TOOLS["deep_research"]["input_schema"]
        assert "query" in schema["properties"]
        assert "query" in schema.get("required", [])

    def test_deep_research_has_format_instructions(self):
        """deep_research 스키마에 format_instructions 선택 필드 포함."""
        from app.services import tool_registry as tr
        schema = tr._TOOLS["deep_research"]["input_schema"]
        assert "format_instructions" in schema["properties"]
        # format_instructions는 required가 아님
        assert "format_instructions" not in schema.get("required", [])

    def test_deep_research_not_in_ptc_callable(self):
        """deep_research는 PTC CALLABLE_TOOLS에서 제외 (자체가 비동기 에이전트)."""
        from app.services.ptc_executor import CALLABLE_TOOLS
        assert "deep_research" not in CALLABLE_TOOLS


# ─── intent_router deep_research 인텐트 테스트 ───────────────────────────────

class TestDeepResearchIntentRouting:
    """intent_router가 딥리서치 키워드를 올바르게 분류."""

    def test_deep_research_intent_map_exists(self):
        """INTENT_MAP에 deep_research 정의됨."""
        from app.services.intent_router import INTENT_MAP
        assert "deep_research" in INTENT_MAP

    def test_deep_research_model_is_gemini(self):
        """deep_research 인텐트 → Gemini 모델 사용."""
        from app.services.intent_router import INTENT_MAP
        entry = INTENT_MAP["deep_research"]
        model = entry.get("model", "")
        assert "gemini" in model.lower()

    def test_딥리서치_keyword_routing(self):
        """'딥리서치' 키워드 → deep_research 인텐트."""
        from app.services.intent_router import _keyword_fallback
        result = _keyword_fallback("AI 시장 딥리서치해줘")
        assert result is not None
        assert result.intent == "deep_research"

    def test_시장분석보고서_keyword_routing(self):
        """'시장 분석 보고서' 키워드 → deep_research 인텐트."""
        from app.services.intent_router import _keyword_fallback
        result = _keyword_fallback("AI 코딩 에이전트 시장 분석 보고서 작성해줘")
        assert result is not None
        assert result.intent == "deep_research"
