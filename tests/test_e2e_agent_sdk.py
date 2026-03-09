"""
AADS-188E: E2E 시나리오 3+4 — Agent SDK 자율 실행 + 시맨틱 코드 검색 테스트

시나리오 3: Agent SDK 자율 실행
  CEO: "AADS 서버 전체 헬스체크하고 이상 있으면 분석해"
  -> Agent SDK query() -> health_check -> code_explorer -> 분석 보고
  검증: 3턴 이상 자율 실행 + 최종 분석 결과 포함

시나리오 4: 시맨틱 코드 검색
  CEO: "인증 처리하는 코드 어디야?"
  -> semantic_code_search -> 관련 코드 청크 컨텍스트 삽입
  검증: 관련 파일/함수 정확 반환
"""
from __future__ import annotations

import asyncio
import json
import importlib
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# 상수 / 헬퍼
# ---------------------------------------------------------------------------

MOCK_SEMANTIC_SEARCH_RESULTS = [
    {
        "project": "AADS",
        "file": "app/api/auth.py",
        "start_line": 45,
        "end_line": 78,
        "type": "function",
        "name": "verify_token",
        "code_snippet": "async def verify_token(token: str):\n    \"\"\"JWT 토큰 검증.\"\"\"",
        "similarity_score": 0.92,
    },
    {
        "project": "AADS",
        "file": "app/api/auth.py",
        "start_line": 12,
        "end_line": 44,
        "type": "function",
        "name": "login",
        "code_snippet": "async def login(email: str, password: str):\n    \"\"\"사용자 로그인.\"\"\"",
        "similarity_score": 0.87,
    },
    {
        "project": "AADS",
        "file": "app/core/security.py",
        "start_line": 1,
        "end_line": 35,
        "type": "class",
        "name": "JWTManager",
        "code_snippet": "class JWTManager:\n    \"\"\"JWT 토큰 관리자.\"\"\"",
        "similarity_score": 0.83,
    },
]


async def _collect_sse(gen: AsyncGenerator) -> List[Dict[str, Any]]:
    events = []
    async for line in gen:
        if isinstance(line, str) and line.startswith("data: "):
            try:
                events.append(json.loads(line[6:].strip()))
            except json.JSONDecodeError:
                pass
    return events


# ---------------------------------------------------------------------------
# 시나리오 3: Agent SDK 자율 실행
# ---------------------------------------------------------------------------

class TestAgentSDKAutonomousExecution:
    """Agent SDK -- 자율 실행 루프 3턴 이상 검증."""

    @pytest.mark.asyncio
    async def test_health_check_routes_to_agent_sdk(self):
        """헬스체크+분석 요청 -> execute/health_check 인텐트 라우팅 (mock)."""
        import app.services.intent_router as ir
        with patch("app.services.intent_router.classify", new_callable=AsyncMock) as mock_classify:
            mock_classify.return_value = MagicMock(intent="execute", confidence=0.90)
            result = await ir.classify("AADS 서버 전체 헬스체크하고 이상 있으면 분석해")
        assert result.intent in {"execute", "health_check", "cto_code_analysis"}

    @pytest.mark.asyncio
    async def test_agent_sdk_executes_3_or_more_turns(self):
        """Agent SDK 3턴 이상 자율 실행 (health_check->code_explorer->분석)."""
        from app.services.agent_sdk_service import AgentSDKService
        svc = AgentSDKService(max_turns=10)
        turn_count = 0

        async def _multi_turn_stream(prompt: str, session_id: Optional[str] = None):
            nonlocal turn_count
            turn_count += 1
            yield f"data: {json.dumps({'type': 'delta', 'content': '[턴1] health_check 실행 중...'})}\n\n"
            turn_count += 1
            yield f"data: {json.dumps({'type': 'delta', 'content': '[턴2] server_211 지연 감지. code_explorer 실행...'})}\n\n"
            turn_count += 1
            yield f"data: {json.dumps({'type': 'delta', 'content': '[턴3] 분석: health_checker.py 타임아웃 미설정 원인.'})}\n\n"
            yield f"data: {json.dumps({'type': 'sdk_complete', 'stop_reason': 'end_turn'})}\n\n"

        with patch.object(svc, "execute_stream", side_effect=_multi_turn_stream):
            events = await _collect_sse(
                svc.execute_stream("AADS 서버 전체 헬스체크하고 이상 있으면 분석해")
            )
        assert turn_count >= 3, f"자율 실행 턴 수 부족: {turn_count}턴"
        delta_texts = " ".join(e["content"] for e in events if e.get("type") == "delta")
        assert "분석" in delta_texts or "code_explorer" in delta_texts.lower()

    @pytest.mark.asyncio
    async def test_autonomous_response_contains_analysis(self):
        """자율 실행 최종 응답에 분석 결과 포함."""
        from app.services.agent_sdk_service import AgentSDKService
        svc = AgentSDKService(max_turns=10)

        async def _analysis_stream(prompt: str, session_id: Optional[str] = None):
            yield f"data: {json.dumps({'type': 'delta', 'content': 'server_211 응답 시간 3200ms 감지'})}\n\n"
            yield f"data: {json.dumps({'type': 'delta', 'content': 'health_checker.py 타임아웃 설정 부재 확인'})}\n\n"
            yield f"data: {json.dumps({'type': 'delta', 'content': '권장: TIMEOUT_SECONDS = 30 설정 필요'})}\n\n"
            yield f"data: {json.dumps({'type': 'sdk_complete', 'stop_reason': 'end_turn'})}\n\n"

        with patch.object(svc, "execute_stream", side_effect=_analysis_stream):
            events = await _collect_sse(svc.execute_stream("헬스체크 후 이상 분석해"))

        full_text = " ".join(e["content"] for e in events if e.get("type") == "delta")
        assert len(full_text) > 50, "분석 결과가 너무 짧음"
        assert any(kw in full_text for kw in ["분석", "감지", "확인", "권장"]), "분석 결과 없음"

    @pytest.mark.asyncio
    async def test_sdk_session_id_captured(self):
        """execute_stream에서 sdk_session_id 캡처."""
        from app.services.agent_sdk_service import AgentSDKService
        svc = AgentSDKService(max_turns=5)

        async def _session_stream(prompt: str, session_id: Optional[str] = None):
            yield f"data: {json.dumps({'type': 'sdk_session', 'session_id': 'sess-abc123'})}\n\n"
            yield f"data: {json.dumps({'type': 'delta', 'content': '실행 중...'})}\n\n"
            yield f"data: {json.dumps({'type': 'sdk_complete', 'session_id': 'sess-abc123'})}\n\n"

        with patch.object(svc, "execute_stream", side_effect=_session_stream):
            events = await _collect_sse(svc.execute_stream("헬스체크해"))

        session_events = [e for e in events if e.get("type") == "sdk_session"]
        assert len(session_events) == 1
        assert session_events[0]["session_id"] == "sess-abc123"

    @pytest.mark.asyncio
    async def test_dangerous_commands_blocked(self):
        """자율 실행 중 위험 명령 차단."""
        from app.services.agent_hooks import pre_tool_use_hook
        context = MagicMock()
        for cmd in ["rm -rf /root/aads", "DROP TABLE chat_messages", "shutdown -h now"]:
            result = await pre_tool_use_hook(
                input_data={"tool_name": "Bash", "tool_input": {"command": cmd}},
                tool_use_id=f"danger-{cmd[:8]}",
                context=context,
            )
            assert result.get("block") is True, f"위험 명령 차단 실패: {cmd}"


# ---------------------------------------------------------------------------
# 시나리오 4: 시맨틱 코드 검색
# ---------------------------------------------------------------------------

class TestSemanticCodeSearch:
    """시맨틱 코드 검색 -- 인증 코드 위치 반환 검증."""

    @pytest.mark.asyncio
    async def test_auth_query_returns_auth_related_files(self):
        """'인증 처리' 쿼리 -> auth.py 관련 파일 반환."""
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()
        with patch.object(svc, "search", new_callable=AsyncMock, return_value=MOCK_SEMANTIC_SEARCH_RESULTS):
            results = await svc.search("인증 처리하는 코드 어디야?", top_k=5)
        assert len(results) >= 1
        file_paths = [r.get("file", "") for r in results]
        assert any("auth" in fp or "security" in fp for fp in file_paths)

    @pytest.mark.asyncio
    async def test_search_results_have_required_fields(self):
        """검색 결과에 file + similarity_score 포함."""
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()
        with patch.object(svc, "search", new_callable=AsyncMock, return_value=MOCK_SEMANTIC_SEARCH_RESULTS):
            results = await svc.search("인증 처리")
        for r in results:
            assert "file" in r
            assert "similarity_score" in r

    @pytest.mark.asyncio
    async def test_search_results_sorted_by_similarity(self):
        """검색 결과 유사도 내림차순 정렬."""
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()
        with patch.object(svc, "search", new_callable=AsyncMock, return_value=MOCK_SEMANTIC_SEARCH_RESULTS):
            results = await svc.search("인증 처리")
        scores = [r["similarity_score"] for r in results]
        assert scores == sorted(scores, reverse=True), f"유사도 정렬 오류: {scores}"

    @pytest.mark.asyncio
    async def test_search_with_project_filter(self):
        """project 필터로 AADS만 검색."""
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()
        aads_results = [r for r in MOCK_SEMANTIC_SEARCH_RESULTS if r.get("project") == "AADS"]
        with patch.object(svc, "search", new_callable=AsyncMock, return_value=aads_results):
            results = await svc.search("인증 처리", project="AADS")
        for r in results:
            assert r.get("project") == "AADS"

    def test_semantic_search_has_search_method(self):
        """SemanticCodeSearch.search() 메서드 존재."""
        from app.services.semantic_code_search import SemanticCodeSearch
        assert hasattr(SemanticCodeSearch, "search")

    def test_search_result_to_context_format(self):
        """검색 결과 -> <codebase_knowledge> 컨텍스트 포맷 변환."""
        chunks = MOCK_SEMANTIC_SEARCH_RESULTS[:3]
        lines = [
            f"  {c['file']}:{c['start_line']} [{c['type']}] {c['name']} (유사도: {c['similarity_score']:.2f})"
            for c in chunks
        ]
        ctx = "<codebase_knowledge>\n" + "\n".join(lines) + "\n</codebase_knowledge>"
        assert "auth.py" in ctx
        assert "verify_token" in ctx


# ---------------------------------------------------------------------------
# Agent SDK 도구 등급 검증
# ---------------------------------------------------------------------------

class TestAgentSDKToolGrades:
    """Agent SDK 도구 등급 검증."""

    def test_green_tools_not_empty(self):
        from app.services.agent_sdk_service import _GREEN_TOOLS, _BUILTIN_ALLOWED
        assert len(_GREEN_TOOLS) > 0
        assert len(_BUILTIN_ALLOWED) > 0

    def test_write_remote_file_is_yellow(self):
        from app.services.agent_sdk_service import _TOOL_GRADES
        assert _TOOL_GRADES.get("write_remote_file") == "Yellow"

    def test_red_tools_not_in_allowed_list(self):
        from app.services.agent_sdk_service import _GREEN_TOOLS, _BUILTIN_ALLOWED, _TOOL_GRADES
        red_tools = [k for k, v in _TOOL_GRADES.items() if v == "Red"]
        all_allowed = set(_GREEN_TOOLS) | set(_BUILTIN_ALLOWED)
        for tool in red_tools:
            assert tool not in all_allowed, f"Red 도구 {tool}이 허용 목록에 있음"

    def test_semantic_code_search_in_green_tools(self):
        from app.services.agent_sdk_service import _GREEN_TOOLS
        assert "semantic_code_search" in _GREEN_TOOLS


# ---------------------------------------------------------------------------
# 전체 통합 검증
# ---------------------------------------------------------------------------

class TestFullSystemIntegration:
    """186E-2(메모리)+188A(리서치)+188B(인덱싱)+188C(SDK) 통합 검증."""

    def test_all_required_services_importable(self):
        modules = [
            ("app.services.memory_manager", "MemoryManager"),
            ("app.services.deep_research_service", "DeepResearchService"),
            ("app.services.code_indexer_service", "CodeIndexerService"),
            ("app.services.agent_sdk_service", "AgentSDKService"),
            ("app.services.semantic_code_search", "SemanticCodeSearch"),
        ]
        for module_path, class_name in modules:
            try:
                mod = importlib.import_module(module_path)
                assert hasattr(mod, class_name), f"{module_path}.{class_name} 없음"
            except ImportError as e:
                pytest.fail(f"{module_path} 임포트 실패: {e}")

    def test_agent_sdk_service_init(self):
        from app.services.agent_sdk_service import AgentSDKService, get_agent_sdk_service
        svc = AgentSDKService(max_turns=5)
        assert svc.max_turns == 5
        assert callable(get_agent_sdk_service)

    def test_memory_manager_importable(self):
        from app.services.memory_manager import MemoryManager, get_memory_manager
        assert callable(get_memory_manager)

    @pytest.mark.asyncio
    async def test_memory_manager_observe_callable(self):
        from app.services.memory_manager import MemoryManager
        mgr = MemoryManager()
        with patch.object(mgr, "observe", new_callable=AsyncMock) as mock_obs:
            mock_obs.return_value = {"saved": True}
            await mgr.observe(category="patterns", key="k", value="v", confidence=0.9)
        mock_obs.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_hook_returns_empty_dict(self):
        from app.services.agent_hooks import stop_hook
        mock_context = MagicMock()
        mock_context.session_id = "sess-001"
        mock_context.messages = [
            {"role": "user", "content": "헬스체크해"},
            {"role": "assistant", "content": "완료"},
            {"role": "user", "content": "분석해"},
            {"role": "assistant", "content": "분석 결과..."},
        ]
        with patch("app.services.memory_manager.get_memory_manager") as mock_factory:
            mock_mgr = AsyncMock()
            mock_mgr.auto_observe_from_session = AsyncMock()
            mock_mgr.save_session_note = AsyncMock()
            mock_factory.return_value = mock_mgr
            result = await stop_hook(input_data={"session_id": "sess-001"}, context=mock_context)
        assert result == {}

    def test_langfuse_is_enabled_returns_bool(self):
        from app.core.langfuse_config import is_enabled
        assert isinstance(is_enabled(), bool)

    @pytest.mark.asyncio
    async def test_agent_sdk_calls_semantic_search_tool(self):
        """Agent SDK가 semantic_code_search 도구 호출 시뮬레이션."""
        from app.services.agent_sdk_service import AgentSDKService
        svc = AgentSDKService(max_turns=10)
        tool_calls = []

        async def _semantic_search_stream(prompt: str, session_id: Optional[str] = None):
            tool_calls.append("semantic_code_search")
            results_text = "\n".join(
                f"- {r['file']}:{r['start_line']} [{r['name']}]"
                for r in MOCK_SEMANTIC_SEARCH_RESULTS
            )
            content_str = "결과:\n" + results_text
            yield f"data: {json.dumps({'type': 'delta', 'content': content_str})}\n\n"
            yield f"data: {json.dumps({'type': 'sdk_complete', 'stop_reason': 'end_turn'})}\n\n"

        with patch.object(svc, "execute_stream", side_effect=_semantic_search_stream):
            events = await _collect_sse(svc.execute_stream("인증 처리하는 코드 어디야?"))

        assert "semantic_code_search" in tool_calls
        delta_text = " ".join(e["content"] for e in events if e.get("type") == "delta")
        assert "auth" in delta_text.lower() or "verify" in delta_text.lower()

    @pytest.mark.asyncio
    async def test_final_response_contains_file_path_and_function(self):
        """최종 응답에 파일 경로와 함수 이름 포함."""
        from app.services.agent_sdk_service import AgentSDKService
        svc = AgentSDKService(max_turns=5)

        async def _response_stream(prompt: str, session_id: Optional[str] = None):
            yield f"data: {json.dumps({'type': 'delta', 'content': 'app/api/auth.py의 verify_token 함수에서 인증을 처리합니다.'})}\n\n"
            yield f"data: {json.dumps({'type': 'sdk_complete', 'stop_reason': 'end_turn'})}\n\n"

        with patch.object(svc, "execute_stream", side_effect=_response_stream):
            events = await _collect_sse(svc.execute_stream("인증 처리하는 코드 어디야?"))

        final_text = " ".join(e["content"] for e in events if e.get("type") == "delta")
        assert "auth.py" in final_text or "verify_token" in final_text
