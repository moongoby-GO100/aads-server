"""
AADS-186E2: Programmatic Tool Calling (PTC) 단위 테스트
- PTC 대상 도구에 allowed_callers 설정 확인
- PTC 제외 도구에 allowed_callers 미설정 확인
- PTCExecutor 병렬 실행 + 쓰기 도구 차단 검증
"""
from __future__ import annotations

import os
import sys
import pytest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ─── tool_registry allowed_callers 설정 검증 ──────────────────────────────────

class TestPTCAllowedCallers:
    """PTC 대상/제외 도구의 allowed_callers 설정 확인."""

    # PTC 허용 도구 (allowed_callers 있어야 함)
    PTC_ALLOWED = [
        "list_remote_dir",
        "read_remote_file",
        "health_check",
        "query_database",
        "jina_read",
        "cost_report",
    ]

    # PTC 제외 도구 (allowed_callers 없어야 함 또는 쓰기 도구)
    PTC_EXCLUDED = [
        "generate_directive",
        "deep_research",
        "directive_create",
    ]

    def _get_raw_tool(self, name: str) -> dict:
        from app.services.tool_registry import _TOOLS
        return _TOOLS.get(name, {})

    def test_list_remote_dir_has_allowed_callers(self):
        """list_remote_dir에 allowed_callers 설정."""
        tool = self._get_raw_tool("list_remote_dir")
        assert "allowed_callers" in tool, "list_remote_dir에 allowed_callers 없음"
        assert "code_execution_20250825" in tool["allowed_callers"]

    def test_read_remote_file_has_allowed_callers(self):
        """read_remote_file에 allowed_callers 설정."""
        tool = self._get_raw_tool("read_remote_file")
        assert "allowed_callers" in tool
        assert "code_execution_20250825" in tool["allowed_callers"]

    def test_health_check_has_allowed_callers(self):
        """health_check에 allowed_callers 설정."""
        tool = self._get_raw_tool("health_check")
        assert "allowed_callers" in tool
        assert "code_execution_20250825" in tool["allowed_callers"]

    def test_query_database_has_allowed_callers(self):
        """query_database에 allowed_callers 설정."""
        tool = self._get_raw_tool("query_database")
        assert "allowed_callers" in tool
        assert "code_execution_20250825" in tool["allowed_callers"]

    def test_jina_read_has_allowed_callers(self):
        """jina_read에 allowed_callers 설정."""
        tool = self._get_raw_tool("jina_read")
        assert "allowed_callers" in tool
        assert "code_execution_20250825" in tool["allowed_callers"]

    def test_cost_report_has_allowed_callers(self):
        """cost_report에 allowed_callers 설정."""
        tool = self._get_raw_tool("cost_report")
        assert "allowed_callers" in tool
        assert "code_execution_20250825" in tool["allowed_callers"]

    def test_generate_directive_no_allowed_callers(self):
        """generate_directive — PTC 제외 (쓰기 도구)."""
        tool = self._get_raw_tool("generate_directive")
        # generate_directive는 allowed_callers 없거나 비어 있어야 함
        assert "allowed_callers" not in tool or not tool.get("allowed_callers")

    def test_deep_research_no_allowed_callers(self):
        """deep_research — PTC 제외 (자체 비동기 에이전트)."""
        tool = self._get_raw_tool("deep_research")
        assert "allowed_callers" not in tool or not tool.get("allowed_callers")

    def test_directive_create_no_allowed_callers(self):
        """directive_create — PTC 제외 (CEO 확인 필요)."""
        tool = self._get_raw_tool("directive_create")
        assert "allowed_callers" not in tool or not tool.get("allowed_callers")

    def test_allowed_callers_excluded_from_api_output(self):
        """get_tools() API 출력에서 allowed_callers 제거 확인."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        tools = registry.get_tools("system")
        for tool in tools:
            assert "allowed_callers" not in tool, f"{tool.get('name')}: allowed_callers API 노출됨"

    def test_get_eager_tools_no_allowed_callers(self):
        """get_eager_tools() 출력에서 allowed_callers 제거 확인."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        tools = registry.get_eager_tools()
        for tool in tools:
            assert "allowed_callers" not in tool


# ─── PTCExecutor 테스트 ───────────────────────────────────────────────────────

class TestPTCExecutor:
    """PTCExecutor 병렬 실행 및 안전 검증."""

    def test_callable_tools_list(self):
        """CALLABLE_TOOLS에 읽기 전용 도구 포함 확인."""
        from app.services.ptc_executor import CALLABLE_TOOLS
        assert "list_remote_dir" in CALLABLE_TOOLS
        assert "read_remote_file" in CALLABLE_TOOLS
        assert "health_check" in CALLABLE_TOOLS
        assert "query_database" in CALLABLE_TOOLS

    def test_write_tools_not_in_callable(self):
        """쓰기 도구가 CALLABLE_TOOLS에 없음."""
        from app.services.ptc_executor import CALLABLE_TOOLS, _WRITE_TOOLS
        for wt in _WRITE_TOOLS:
            assert wt not in CALLABLE_TOOLS, f"쓰기 도구 {wt}가 CALLABLE_TOOLS에 포함됨"

    @pytest.mark.asyncio
    async def test_write_tool_blocked(self):
        """쓰기 도구 호출 시 에러 처리."""
        from app.services.ptc_executor import PTCExecutor, PTCToolCall
        executor = PTCExecutor()
        calls = [PTCToolCall(tool_name="directive_create", tool_input={})]
        result = await executor.execute_parallel(calls)
        assert len(result.errors) > 0
        assert any("PTC 거부" in e for e in result.errors)
        assert result.final_output  # 에러 메시지 포함

    @pytest.mark.asyncio
    async def test_unknown_tool_blocked(self):
        """CALLABLE_TOOLS에 없는 도구 차단."""
        from app.services.ptc_executor import PTCExecutor, PTCToolCall
        executor = PTCExecutor()
        calls = [PTCToolCall(tool_name="nonexistent_tool", tool_input={})]
        result = await executor.execute_parallel(calls)
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_parallel_execution_health_check(self):
        """health_check 병렬 실행 (mock)."""
        from app.services.ptc_executor import PTCExecutor, PTCToolCall

        mock_result = '{"status": "ok", "server": "68"}'

        with patch("app.services.tool_executor.ToolExecutor.execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_result
            executor = PTCExecutor()
            calls = [
                PTCToolCall("health_check", {"server": "68"}, alias="s68"),
                PTCToolCall("health_check", {"server": "211"}, alias="s211"),
            ]
            result = await executor.execute_parallel(calls)

        assert "s68" in result.results
        assert "s211" in result.results
        assert mock_exec.call_count == 2

    @pytest.mark.asyncio
    async def test_token_estimate_positive_for_parallel(self):
        """병렬 실행 시 토큰 절감 추정값이 양수."""
        from app.services.ptc_executor import PTCExecutor, PTCToolCall

        with patch("app.services.tool_executor.ToolExecutor.execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = "ok"
            executor = PTCExecutor()
            calls = [
                PTCToolCall("health_check", {"server": "68"}, alias="s68"),
                PTCToolCall("health_check", {"server": "211"}, alias="s211"),
                PTCToolCall("health_check", {"server": "114"}, alias="s114"),
            ]
            result = await executor.execute_parallel(calls)

        assert result.token_estimate > 0

    def test_ptc_result_dataclass_fields(self):
        """PTCResult 필드 구조 확인."""
        from app.services.ptc_executor import PTCResult
        r = PTCResult()
        assert isinstance(r.results, dict)
        assert isinstance(r.errors, list)
        assert isinstance(r.final_output, str)
        assert isinstance(r.token_estimate, int)


# ─── code_execution 도구 설정 테스트 ─────────────────────────────────────────

class TestCodeExecutionTool:
    """code_execution 도구 등록 확인."""

    def test_code_execution_registered(self):
        """code_execution 도구가 tool_registry에 등록됨."""
        from app.services.tool_registry import _TOOLS
        assert "code_execution" in _TOOLS

    def test_code_execution_type(self):
        """code_execution type = code_execution_20250825."""
        from app.services.tool_registry import _TOOLS
        tool = _TOOLS["code_execution"]
        assert tool.get("type") == "code_execution_20250825"

    def test_run_parallel_health_check_utility(self):
        """run_parallel_health_check 유틸 함수 존재 확인."""
        from app.services.ptc_executor import run_parallel_health_check
        import asyncio
        assert callable(run_parallel_health_check)
