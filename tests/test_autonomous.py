"""
AADS-186E-3: 자율 실행 루프 단위 테스트
- 자율 루프 max_iterations=3 → 3회 내 종료 확인
- 비용 상한 초과 → cost_limit 이벤트 생성 확인
- submit_directive 호출 시 confirm_required 이벤트 확인
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def collect_events(gen: AsyncGenerator) -> List[Dict[str, Any]]:
    """AsyncGenerator에서 SSE 이벤트를 동기적으로 수집."""
    async def _collect():
        events = []
        async for line in gen:
            if line.startswith("data: "):
                try:
                    data = json.loads(line.replace("data: ", "").strip())
                    events.append(data)
                except Exception:
                    pass
        return events
    return run(_collect())


# ─── mock LLM call_stream ────────────────────────────────────────────────────

def _make_mock_call_stream(responses: List[str], use_tools: bool = False, tool_name: str = "health_check"):
    """LLM call_stream mock 생성."""
    call_count = [0]

    async def _mock_call_stream(intent_result, system_prompt, messages, tools=None, model_override=None):
        idx = min(call_count[0], len(responses) - 1)
        text = responses[idx]
        call_count[0] += 1

        if use_tools and tools and call_count[0] == 1:
            # 첫 번째 호출에서 도구 사용
            yield {"type": "tool_use", "tool_name": tool_name, "tool_use_id": f"tu_{idx}", "tool_input": {}}
            yield {"type": "done", "input_tokens": 100, "output_tokens": 50, "stop_reason": "tool_use"}
        else:
            yield {"type": "delta", "content": text}
            yield {"type": "done", "input_tokens": 100, "output_tokens": 50, "stop_reason": "end_turn"}

    return _mock_call_stream


class TestAutonomousExecutorBasic:
    """AutonomousExecutor 기본 동작 테스트."""

    def test_executor_singleton(self):
        """get_autonomous_executor() 호출 시 싱글턴 반환."""
        from app.services.autonomous_executor import get_autonomous_executor
        a = get_autonomous_executor()
        b = get_autonomous_executor()
        assert a is b

    def test_executor_defaults(self):
        """기본 MAX_ITERATIONS=25, COST_LIMIT=$2.0 확인."""
        from app.services.autonomous_executor import AutonomousExecutor
        exec_ = AutonomousExecutor()
        assert exec_.max_iterations == 25
        assert exec_.cost_limit == 2.0

    def test_executor_custom_params(self):
        """커스텀 파라미터 지정 가능."""
        from app.services.autonomous_executor import AutonomousExecutor
        exec_ = AutonomousExecutor(max_iterations=3, cost_limit=0.5)
        assert exec_.max_iterations == 3
        assert exec_.cost_limit == 0.5


class TestAutonomousExecutorLoop:
    """자율 루프 실행 테스트."""

    def test_complete_without_tools(self):
        """도구 없는 단순 응답 → complete 이벤트 생성."""
        from app.services.autonomous_executor import AutonomousExecutor

        exec_ = AutonomousExecutor(max_iterations=3)
        mock_stream = _make_mock_call_stream(["안녕하세요! 작업을 완료했습니다."])

        with patch("app.services.autonomous_executor.call_stream", new=mock_stream):
            events = collect_events(exec_.execute_task(
                task_description="간단한 인사 작업",
                tools=[],
                messages=[{"role": "user", "content": "안녕"}],
            ))

        event_types = [e.get("type") for e in events]
        assert "complete" in event_types

    def test_max_iterations_reached(self):
        """max_iterations=3이고 계속 도구 사용 → max_iterations 이벤트 생성."""
        from app.services.autonomous_executor import AutonomousExecutor

        exec_ = AutonomousExecutor(max_iterations=3)

        # 항상 도구를 사용하는 mock
        async def _always_tool_stream(intent_result, system_prompt, messages, tools=None, model_override=None):
            yield {"type": "tool_use", "tool_name": "health_check", "tool_use_id": "tu_1", "tool_input": {}}
            yield {"type": "done", "input_tokens": 50, "output_tokens": 30, "stop_reason": "tool_use"}

        _tools = [{"name": "health_check", "description": "헬스체크"}]

        with (
            patch("app.services.autonomous_executor.call_stream", new=_always_tool_stream),
            patch("app.services.autonomous_executor.ToolExecutor") as mock_te,
        ):
            mock_te.return_value.execute = AsyncMock(return_value='{"status": "ok"}')
            events = collect_events(exec_.execute_task(
                task_description="헬스체크 반복",
                tools=_tools,
                messages=[{"role": "user", "content": "헬스체크"}],
            ))

        event_types = [e.get("type") for e in events]
        assert "max_iterations" in event_types

        # max_iterations 이벤트에 iterations 필드 확인
        max_evt = next(e for e in events if e.get("type") == "max_iterations")
        assert max_evt.get("iterations") == 3

    def test_tool_result_events_emitted(self):
        """도구 실행 후 tool_result 이벤트 SSE 포함."""
        from app.services.autonomous_executor import AutonomousExecutor

        exec_ = AutonomousExecutor(max_iterations=3)
        mock_stream = _make_mock_call_stream(["완료"], use_tools=True, tool_name="health_check")
        _tools = [{"name": "health_check", "description": "헬스체크"}]

        with (
            patch("app.services.autonomous_executor.call_stream", new=mock_stream),
            patch("app.services.autonomous_executor.ToolExecutor") as mock_te,
        ):
            mock_te.return_value.execute = AsyncMock(return_value='{"status": "ok"}')
            events = collect_events(exec_.execute_task(
                task_description="",
                tools=_tools,
                messages=[{"role": "user", "content": "상태 확인"}],
            ))

        event_types = [e.get("type") for e in events]
        assert "tool_use" in event_types
        assert "tool_result" in event_types


class TestCostLimitEnforcement:
    """비용 상한 테스트."""

    def test_cost_limit_blocks_execution(self):
        """누적 비용이 limit 초과 → cost_limit 이벤트 생성."""
        from app.services.autonomous_executor import AutonomousExecutor, _calc_cost

        # 낮은 비용 상한 설정
        exec_ = AutonomousExecutor(max_iterations=25, cost_limit=0.000001)

        async def _tool_stream(intent_result, system_prompt, messages, tools=None, model_override=None):
            yield {"type": "tool_use", "tool_name": "health_check", "tool_use_id": "tu_x", "tool_input": {}}
            # 많은 토큰으로 비용 초과 유도
            yield {"type": "done", "input_tokens": 1000, "output_tokens": 500, "stop_reason": "tool_use"}

        _tools = [{"name": "health_check"}]

        with (
            patch("app.services.autonomous_executor.call_stream", new=_tool_stream),
            patch("app.services.autonomous_executor.ToolExecutor") as mock_te,
        ):
            mock_te.return_value.execute = AsyncMock(return_value='{}')
            events = collect_events(exec_.execute_task(
                task_description="",
                tools=_tools,
                messages=[{"role": "user", "content": "test"}],
            ))

        event_types = [e.get("type") for e in events]
        assert "cost_limit" in event_types

        cost_evt = next(e for e in events if e.get("type") == "cost_limit")
        assert cost_evt.get("total_cost") is not None
        assert cost_evt.get("total_cost") > 0


class TestDangerousToolBlocking:
    """위험 도구 차단 테스트."""

    def test_submit_directive_blocked(self):
        """submit_directive 도구 호출 시 confirm_required 이벤트 + 실제 실행 안됨."""
        from app.services.autonomous_executor import AutonomousExecutor

        exec_ = AutonomousExecutor(max_iterations=3)

        async def _directive_stream(intent_result, system_prompt, messages, tools=None, model_override=None):
            yield {
                "type": "tool_use",
                "tool_name": "submit_directive",
                "tool_use_id": "tu_danger",
                "tool_input": {"content": "위험한 지시서"},
            }
            yield {"type": "done", "input_tokens": 50, "output_tokens": 30, "stop_reason": "tool_use"}

        _tools = [{"name": "submit_directive", "description": "지시서 제출"}]

        real_executions = []

        with (
            patch("app.services.autonomous_executor.call_stream", new=_directive_stream),
            patch("app.services.autonomous_executor.ToolExecutor") as mock_te,
        ):
            async def _track_execute(name, inp):
                real_executions.append(name)
                return "{}"
            mock_te.return_value.execute = AsyncMock(side_effect=_track_execute)

            events = collect_events(exec_.execute_task(
                task_description="",
                tools=_tools,
                messages=[{"role": "user", "content": "지시서 실행"}],
            ))

        event_types = [e.get("type") for e in events]
        assert "confirm_required" in event_types

        # submit_directive는 실제 실행되지 않아야 함
        assert "submit_directive" not in real_executions

    def test_confirm_required_message(self):
        """confirm_required 이벤트에 tool_name, message 필드 포함."""
        from app.services.autonomous_executor import AutonomousExecutor

        exec_ = AutonomousExecutor(max_iterations=2)

        async def _dangerous_stream(intent_result, system_prompt, messages, tools=None, model_override=None):
            yield {
                "type": "tool_use",
                "tool_name": "directive_create",
                "tool_use_id": "tu_d",
                "tool_input": {},
            }
            yield {"type": "done", "input_tokens": 10, "output_tokens": 10, "stop_reason": "tool_use"}

        _tools = [{"name": "directive_create"}]

        with (
            patch("app.services.autonomous_executor.call_stream", new=_dangerous_stream),
            patch("app.services.autonomous_executor.ToolExecutor") as mock_te,
        ):
            mock_te.return_value.execute = AsyncMock(return_value="{}")
            events = collect_events(exec_.execute_task(
                task_description="",
                tools=_tools,
                messages=[{"role": "user", "content": "지시서"}],
            ))

        confirm_evt = next((e for e in events if e.get("type") == "confirm_required"), None)
        assert confirm_evt is not None
        assert confirm_evt.get("tool_name") == "directive_create"
        assert confirm_evt.get("message")
