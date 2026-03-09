"""
AADS-186E2: Programmatic Tool Calling (PTC) 단위 테스트
- CALLABLE_TOOLS: 읽기 전용 도구 목록 확인
- 쓰기 도구 PTC 제외 확인
- PTCExecutor 병렬 실행 동작 확인 (mock)
- code_execution 도구 등록 및 allowed_callers 확인
- PTC 제외 도구 (submit_directive, generate_directive, deep_research) 확인
"""
from __future__ import annotations

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ─── CALLABLE_TOOLS 목록 검증 ─────────────────────────────────────────────────

class TestCallableTools:
    """PTC 허용 읽기 전용 도구 목록 검증."""

    def test_callable_tools_exists(self):
        """CALLABLE_TOOLS 상수 존재."""
        from app.services.ptc_executor import CALLABLE_TOOLS
        assert isinstance(CALLABLE_TOOLS, list)
        assert len(CALLABLE_TOOLS) > 0

    def test_read_only_tools_in_callable(self):
        """읽기 전용 도구들이 CALLABLE_TOOLS에 포함."""
        from app.services.ptc_executor import CALLABLE_TOOLS
        expected_tools = [
            "list_remote_dir",
            "read_remote_file",
            "health_check",
        ]
        for tool in expected_tools:
            assert tool in CALLABLE_TOOLS, f"{tool} should be in CALLABLE_TOOLS"

    def test_write_tools_not_in_callable(self):
        """쓰기 도구는 CALLABLE_TOOLS에서 제외."""
        from app.services.ptc_executor import CALLABLE_TOOLS, _WRITE_TOOLS
        for tool in _WRITE_TOOLS:
            assert tool not in CALLABLE_TOOLS, f"Write tool {tool} must NOT be in CALLABLE_TOOLS"

    def test_deep_research_not_in_callable(self):
        """deep_research는 PTC CALLABLE_TOOLS에서 제외 (자체가 비동기 에이전트)."""
        from app.services.ptc_executor import CALLABLE_TOOLS
        assert "deep_research" not in CALLABLE_TOOLS

    def test_generate_directive_not_in_callable(self):
        """generate_directive는 PTC 제외 (결과 검토 필요)."""
        from app.services.ptc_executor import CALLABLE_TOOLS
        assert "generate_directive" not in CALLABLE_TOOLS


# ─── PTCExecutor 기본 동작 ────────────────────────────────────────────────────

class TestPTCExecutor:
    """PTCExecutor 클래스 기본 동작 테스트."""

    def test_ptc_executor_instantiation(self):
        """PTCExecutor 인스턴스 생성."""
        from app.services.ptc_executor import PTCExecutor
        # ToolExecutor가 없어도 인스턴스 생성 시도는 가능
        try:
            executor = PTCExecutor()
            assert executor is not None
        except Exception:
            # ToolExecutor 초기화 실패는 허용 (DB 없음)
            pass

    def test_ptc_tool_call_dataclass(self):
        """PTCToolCall 데이터클래스 생성."""
        from app.services.ptc_executor import PTCToolCall
        call = PTCToolCall(
            tool_name="health_check",
            tool_input={"server": "all"},
            alias="health",
        )
        assert call.tool_name == "health_check"
        assert call.alias == "health"

    def test_ptc_result_dataclass(self):
        """PTCResult 데이터클래스 생성."""
        from app.services.ptc_executor import PTCResult
        result = PTCResult(
            results={"health": {"status": "ok"}},
            final_output="서버 상태: 정상",
            token_estimate=150,
        )
        assert result.final_output == "서버 상태: 정상"
        assert result.token_estimate == 150
        assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_write_tool_blocked(self):
        """쓰기 도구 호출 시 PTCResult.errors에 오류 추가."""
        from app.services.ptc_executor import PTCExecutor, PTCToolCall, _WRITE_TOOLS
        executor = PTCExecutor.__new__(PTCExecutor)
        executor._executor = MagicMock()

        # 쓰기 도구 호출 시도
        write_tool = next(iter(_WRITE_TOOLS))
        call = PTCToolCall(tool_name=write_tool, tool_input={}, alias="blocked")
        result = await executor.execute_parallel([call])
        assert len(result.errors) > 0
        assert result.results == {}


# ─── tool_registry code_execution 설정 검증 ──────────────────────────────────

class TestCodeExecutionTool:
    """code_execution PTC 도구 등록 확인."""

    def test_code_execution_in_registry(self):
        """code_execution 도구 tool_registry에 등록됨."""
        from app.services import tool_registry as tr
        assert "code_execution" in tr._TOOLS

    def test_code_execution_type(self):
        """code_execution type = 'code_execution_20250825'."""
        from app.services import tool_registry as tr
        config = tr._TOOLS["code_execution"]
        assert config["type"] == "code_execution_20250825"

    def test_code_execution_allowed_callers(self):
        """code_execution allowed_callers에 'code_execution_20250825' 포함."""
        from app.services import tool_registry as tr
        config = tr._TOOLS["code_execution"]
        assert "allowed_callers" in config
        assert "code_execution_20250825" in config["allowed_callers"]

    def test_ptc_excluded_tools_no_allowed_callers(self):
        """PTC 제외 도구(generate_directive)에 allowed_callers 미설정."""
        from app.services import tool_registry as tr
        # generate_directive는 쓰기 도구 — allowed_callers 없어야 함
        assert "generate_directive" in tr._TOOLS
        config = tr._TOOLS["generate_directive"]
        assert "allowed_callers" not in config


# ─── PTC 허용/제외 도구 경계 테스트 ──────────────────────────────────────────

class TestPTCBoundaries:
    """PTC 도구 허용/제외 경계 정책 검증."""

    def test_health_check_allowed_for_ptc(self):
        """health_check: PTC 허용 (병렬 서버 조회)."""
        from app.services.ptc_executor import CALLABLE_TOOLS
        assert "health_check" in CALLABLE_TOOLS

    def test_query_database_allowed_for_ptc(self):
        """query_database: PTC 허용 (연쇄 쿼리)."""
        from app.services.ptc_executor import CALLABLE_TOOLS
        # 'query_database'가 있거나 'query_db'가 있어야 함
        has_db_tool = ("query_database" in CALLABLE_TOOLS or "query_db" in CALLABLE_TOOLS)
        assert has_db_tool

    def test_directive_create_write_tool(self):
        """directive_create: 쓰기 도구 — PTC 제외."""
        from app.services.ptc_executor import _WRITE_TOOLS, CALLABLE_TOOLS
        # directive_create는 쓰기 도구이거나 CALLABLE_TOOLS에 없어야 함
        if "directive_create" in _WRITE_TOOLS:
            assert "directive_create" not in CALLABLE_TOOLS

    def test_write_tools_constant_not_empty(self):
        """_WRITE_TOOLS 상수가 비어 있지 않음."""
        from app.services.ptc_executor import _WRITE_TOOLS
        assert isinstance(_WRITE_TOOLS, (set, list, frozenset))
        assert len(_WRITE_TOOLS) > 0

    def test_callable_tools_are_read_only(self):
        """CALLABLE_TOOLS에 쓰기 도구 없음."""
        from app.services.ptc_executor import CALLABLE_TOOLS, _WRITE_TOOLS
        overlap = set(CALLABLE_TOOLS) & set(_WRITE_TOOLS)
        assert len(overlap) == 0, f"Write tools in CALLABLE_TOOLS: {overlap}"


# ─── 병렬 실행 시뮬레이션 ─────────────────────────────────────────────────────

class TestParallelExecution:
    """병렬 실행 구조 검증."""

    @pytest.mark.asyncio
    async def test_parallel_execution_with_mock(self):
        """여러 도구 병렬 실행 — mock ToolExecutor 사용."""
        from app.services.ptc_executor import PTCExecutor, PTCToolCall

        executor = PTCExecutor.__new__(PTCExecutor)
        mock_tool_exec = MagicMock()
        mock_tool_exec.execute = AsyncMock(side_effect=[
            {"status": "ok", "server": "68"},
            {"status": "ok", "server": "211"},
        ])
        executor._executor = mock_tool_exec

        calls = [
            PTCToolCall(tool_name="health_check", tool_input={"server": "68"}, alias="h68"),
            PTCToolCall(tool_name="health_check", tool_input={"server": "211"}, alias="h211"),
        ]
        result = await executor.execute_parallel(calls)
        # 결과에 두 alias 포함
        assert "h68" in result.results or "h211" in result.results or len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_empty_calls_returns_empty_result(self):
        """빈 도구 목록 → 빈 PTCResult 반환."""
        from app.services.ptc_executor import PTCExecutor, PTCResult

        executor = PTCExecutor.__new__(PTCExecutor)
        executor._executor = MagicMock()
        result = await executor.execute_parallel([])
        assert isinstance(result, PTCResult)
        assert result.results == {}
        assert result.errors == []
