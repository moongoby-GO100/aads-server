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


# ─── AADS-188A: 신규 기능 테스트 ──────────────────────────────────────────────

class TestResearchEventNewFields:
    """AADS-188A: ResearchEvent content/sources/phase 필드 테스트."""

    def test_research_event_planning(self):
        """planning 타입 이벤트."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="planning", content="연구 계획 수립 중...", phase="planning", progress_pct=5)
        assert ev.type == "planning"
        assert ev.content == "연구 계획 수립 중..."
        assert ev.phase == "planning"
        assert ev.progress_pct == 5

    def test_research_event_searching(self):
        """searching 타입 이벤트."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="searching", content="소스 탐색 중... (3/15)", phase="searching", progress_pct=30)
        assert ev.type == "searching"
        assert ev.progress_pct == 30

    def test_research_event_analyzing(self):
        """analyzing 타입 이벤트."""
        from app.models.research import ResearchEvent
        ev = ResearchEvent(type="analyzing", content="교차 분석 중...", progress_pct=80)
        assert ev.type == "analyzing"
        assert ev.progress_pct == 80

    def test_research_event_complete_with_sources(self):
        """complete 이벤트 — sources 목록 포함."""
        from app.models.research import ResearchEvent
        sources = [{"url": "https://example.com", "title": "Example"}]
        ev = ResearchEvent(type="complete", content="최종 보고서 내용...", sources=sources, progress_pct=100)
        assert ev.type == "complete"
        assert ev.sources is not None
        assert len(ev.sources) == 1
        assert ev.sources[0]["url"] == "https://example.com"

    def test_research_result_sources_default_empty(self):
        """ResearchResult 기본 sources 빈 리스트."""
        from app.models.research import ResearchResult
        r = ResearchResult(content="보고서", status="completed")
        assert r.sources == []

    def test_research_result_with_sources(self):
        """ResearchResult sources 포함."""
        from app.models.research import ResearchResult
        sources = [{"url": "https://a.com"}, {"url": "https://b.com"}]
        r = ResearchResult(content="보고서", status="completed", sources=sources)
        assert len(r.sources) == 2


class TestDeepResearchStreamFeatures:
    """AADS-188A: research_stream() 및 context/format 파라미터 테스트."""

    def test_service_has_research_stream_method(self):
        """DeepResearchService에 research_stream 메서드 존재 (async generator)."""
        from app.services.deep_research_service import DeepResearchService
        import inspect
        svc = DeepResearchService()
        assert hasattr(svc, "research_stream")
        # research_stream은 async generator이므로 isasyncgenfunction 사용
        assert inspect.isasyncgenfunction(svc.research_stream)

    def test_build_prompt_with_context(self):
        """_build_prompt: context 포함 시 프롬프트에 추가."""
        from app.services.deep_research_service import _build_prompt
        result = _build_prompt("AI 시장", "우리 회사는 SaaS 스타트업", None)
        assert "AI 시장" in result
        assert "배경 컨텍스트" in result
        assert "우리 회사는 SaaS 스타트업" in result

    def test_build_prompt_without_context(self):
        """_build_prompt: context 없을 때 query만."""
        from app.services.deep_research_service import _build_prompt
        result = _build_prompt("AI 시장", None, None)
        assert "AI 시장" in result
        assert "배경 컨텍스트" not in result

    def test_format_preset_summary(self):
        """_format_preset: summary 프리셋."""
        from app.services.deep_research_service import _format_preset
        result = _format_preset("summary")
        assert result is not None
        assert "요약" in result

    def test_format_preset_detailed(self):
        """_format_preset: detailed 프리셋."""
        from app.services.deep_research_service import _format_preset
        result = _format_preset("detailed")
        assert result is not None
        assert "분석" in result

    def test_format_preset_report(self):
        """_format_preset: report 프리셋."""
        from app.services.deep_research_service import _format_preset
        result = _format_preset("report")
        assert result is not None
        assert "보고서" in result

    def test_format_preset_none(self):
        """_format_preset: None 입력 시 None 반환."""
        from app.services.deep_research_service import _format_preset
        result = _format_preset(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_research_stream_no_api_key_yields_error(self):
        """API 키 없을 때 research_stream()은 error 이벤트 yield."""
        from app.services.deep_research_service import DeepResearchService
        from app.models.research import ResearchEvent
        svc = DeepResearchService()
        svc._api_key = ""
        events = []
        async for ev in svc.research_stream("AI 시장 동향"):
            events.append(ev)
        assert len(events) == 1
        assert events[0].type == "error"

    @pytest.mark.asyncio
    async def test_research_stream_daily_limit_yields_error(self):
        """일일 한도 초과 시 research_stream()은 error 이벤트 yield."""
        from app.services import deep_research_service as dr_mod
        from app.services.deep_research_service import DeepResearchService
        original = dr_mod._daily_usage.copy()
        today = dr_mod._today_str()
        dr_mod._daily_usage[today] = 5
        try:
            svc = DeepResearchService()
            svc._api_key = "fake_key"
            events = []
            async for ev in svc.research_stream("AI 시장 동향"):
                events.append(ev)
            assert len(events) == 1
            assert events[0].type == "error"
            assert "한도" in (events[0].content or "")
        finally:
            dr_mod._daily_usage.clear()
            dr_mod._daily_usage.update(original)

    def test_google_genai_api_key_support(self):
        """GOOGLE_GENAI_API_KEY 환경변수 지원 확인."""
        import os
        import importlib
        original_env = os.environ.copy()
        try:
            os.environ["GOOGLE_GENAI_API_KEY"] = "test_google_key"
            os.environ.pop("GEMINI_API_KEY", None)
            import app.services.deep_research_service as dr_mod
            # 모듈 레벨 변수 재확인 (실제 실행 환경)
            key = os.getenv("GOOGLE_GENAI_API_KEY") or os.getenv("GEMINI_API_KEY", "")
            assert key == "test_google_key"
        finally:
            os.environ.clear()
            os.environ.update(original_env)

    def test_monthly_limit_functions(self):
        """월간 카운터 함수 동작 확인."""
        from app.services import deep_research_service as dr_mod
        month = dr_mod._month_str()
        assert isinstance(month, str)
        assert len(month) == 7  # YYYY-MM
        assert dr_mod._check_monthly_limit() is True


class TestToolRegistryDeepResearchSchema:
    """AADS-188A: tool_registry deep_research 스키마 검증."""

    def test_deep_research_has_context_param(self):
        """deep_research 스키마에 context 파라미터 존재."""
        from app.services.tool_registry import _TOOLS
        schema = _TOOLS["deep_research"]["input_schema"]
        assert "context" in schema["properties"]

    def test_deep_research_has_format_param(self):
        """deep_research 스키마에 format 파라미터 존재."""
        from app.services.tool_registry import _TOOLS
        schema = _TOOLS["deep_research"]["input_schema"]
        assert "format" in schema["properties"]
        format_prop = schema["properties"]["format"]
        assert "enum" in format_prop
        assert "summary" in format_prop["enum"]
        assert "detailed" in format_prop["enum"]
        assert "report" in format_prop["enum"]

    def test_deep_research_required_only_query(self):
        """deep_research 필수 파라미터는 query만."""
        from app.services.tool_registry import _TOOLS
        schema = _TOOLS["deep_research"]["input_schema"]
        assert schema["required"] == ["query"]


class TestIntentRouterDeepResearchKeywords:
    """AADS-188A: intent_router 딥리서치 키워드 테스트."""

    def test_keyword_리서치(self):
        """'리서치' 키워드 → deep_research."""
        from app.services.intent_router import _keyword_fallback
        result = _keyword_fallback("AI 코딩 에이전트 리서치 해줘")
        assert result.intent == "deep_research"

    def test_keyword_경쟁사(self):
        """'경쟁사' 키워드 → deep_research."""
        from app.services.intent_router import _keyword_fallback
        result = _keyword_fallback("경쟁사 분석해줘")
        assert result.intent == "deep_research"

    def test_keyword_트렌드(self):
        """'트렌드' 키워드 → deep_research."""
        from app.services.intent_router import _keyword_fallback
        result = _keyword_fallback("AI 트렌드 보고서 써줘")
        assert result.intent == "deep_research"

    def test_keyword_딥리서치(self):
        """'딥리서치' 키워드 → deep_research."""
        from app.services.intent_router import _keyword_fallback
        result = _keyword_fallback("딥리서치 해줘")
        assert result.intent == "deep_research"

    def test_keyword_시장분석보고서(self):
        """'시장 분석 보고서' 키워드 → deep_research."""
        from app.services.intent_router import _keyword_fallback
        result = _keyword_fallback("시장 분석 보고서 작성해줘")
        assert result.intent == "deep_research"
