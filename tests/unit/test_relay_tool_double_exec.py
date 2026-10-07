"""chat.relay_tool_double_exec — relay 가 이미 실행한 tool_use 를 바깥 루프가 다시 실행하던 결함.

2026-10-07 세션 fb1b5a3e: AutonomousExecutor 가 call_stream(relay) 의 tool_use 를 "실행할 도구"로
읽어 ToolExecutor 로 한 번 더 실행하고(`unknown_tool: ToolSearch`), 그 결과를 user 메시지로
되먹여 relay 새 턴을 시작시켰다.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import model_selector, tool_executor


def _run(coro):
    # asyncio.run 은 종료 시 현재 이벤트 루프를 None 으로 만들어 get_event_loop() 를 쓰는 다른 테스트를 깨뜨린다.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean_ledger():
    tool_executor._EXECUTED_TOOL_USE_IDS.clear()
    yield
    tool_executor._EXECUTED_TOOL_USE_IDS.clear()


def _run_executor(stream, *, execute_mock: AsyncMock) -> List[Dict[str, Any]]:
    from app.services.autonomous_executor import AutonomousExecutor

    fake_te = MagicMock()
    fake_te.return_value.execute = execute_mock

    async def _collect():
        out = []
        with patch("app.services.autonomous_executor.call_stream", new=stream), \
                patch("app.services.autonomous_executor.ToolExecutor", new=fake_te):
            async for line in AutonomousExecutor(max_iterations=4).execute_task(
                task_description="",
                tools=[{"name": "todo_write", "description": "t"}],
                messages=[{"role": "user", "content": "진행해"}],
                session_id="11111111-1111-1111-1111-111111111111",
            ):
                if line.startswith("data: "):
                    try:
                        out.append(json.loads(line[6:].strip()))
                    except Exception:
                        pass
        return out

    return _run(_collect())


def _relay_stream(calls: List[Dict[str, Any]], *, flagged: bool = True, with_result: bool = True):
    """relay 가 도구를 직접 실행한 응답: done 에 stop_reason 이 없다."""
    count = [0]

    async def _stream(intent_result, system_prompt, messages, tools=None, model_override=None, session_id=None):
        count[0] += 1
        _stream.messages.append(list(messages))
        if count[0] == 1:
            for c in calls:
                ev = {"type": "tool_use", "tool_name": c["name"], "tool_use_id": c["id"], "tool_input": {"x": 1}}
                if flagged:
                    ev["provider_executed"] = True
                yield ev
                if with_result:
                    yield {"type": "tool_result", "tool_name": c["name"], "tool_use_id": c["id"], "content": "ok"}
        yield {"type": "delta", "content": "끝"}
        yield {"type": "done", "input_tokens": 1, "output_tokens": 1}

    _stream.messages = []
    _stream.count = count
    return _stream


def test_relay_tool_use_is_not_reexecuted():
    stream = _relay_stream([{"name": "todo_write", "id": "toolu_A"}, {"name": "ToolSearch", "id": "toolu_B"}])
    execute = AsyncMock(return_value="{}")

    events = _run_executor(stream, execute_mock=execute)

    execute.assert_not_called()
    assert stream.count[0] == 1
    assert any(e.get("type") == "complete" for e in events)


def test_relay_tool_result_keeps_tool_use_id():
    stream = _relay_stream([{"name": "todo_write", "id": "toolu_A"}])

    events = _run_executor(stream, execute_mock=AsyncMock())

    results = [e for e in events if e.get("type") == "tool_result"]
    assert results and results[0]["tool_use_id"] == "toolu_A"


def test_matching_tool_result_blocks_reexecution_even_without_flag():
    stream = _relay_stream([{"name": "todo_write", "id": "toolu_A"}], flagged=False, with_result=True)
    execute = AsyncMock(return_value="{}")

    _run_executor(stream, execute_mock=execute)

    execute.assert_not_called()


def test_relay_never_receives_tool_result_user_message():
    stream = _relay_stream([{"name": "todo_write", "id": "toolu_A"}])

    _run_executor(stream, execute_mock=AsyncMock())

    for msgs in stream.messages:
        assert not model_selector._is_tool_result_only_user_turn(msgs)


def test_unflagged_tool_use_without_result_is_still_executed_once():
    """바깥 루프가 소유하는 도구(provider 가 실행하지 않음)는 종전처럼 실행된다."""
    count = [0]

    async def _stream(intent_result, system_prompt, messages, tools=None, model_override=None, session_id=None):
        count[0] += 1
        if count[0] == 1:
            yield {"type": "tool_use", "tool_name": "todo_write", "tool_use_id": "toolu_C", "tool_input": {}}
            yield {"type": "done", "stop_reason": "tool_use"}
        else:
            yield {"type": "delta", "content": "끝"}
            yield {"type": "done", "stop_reason": "end_turn"}

    execute = AsyncMock(return_value="{}")
    _run_executor(_stream, execute_mock=execute)

    assert execute.await_count == 1


def test_same_tool_use_id_is_never_executed_twice():
    count = [0]

    async def _stream(intent_result, system_prompt, messages, tools=None, model_override=None, session_id=None):
        count[0] += 1
        if count[0] <= 2:
            yield {"type": "tool_use", "tool_name": "todo_write", "tool_use_id": "toolu_D", "tool_input": {}}
            yield {"type": "done", "stop_reason": "tool_use"}
        else:
            yield {"type": "delta", "content": "끝"}
            yield {"type": "done", "stop_reason": "end_turn"}

    execute = AsyncMock(return_value="{}")
    _run_executor(_stream, execute_mock=execute)

    assert execute.await_count == 1


def test_executed_ledger_is_bounded_and_ignores_empty_ids():
    tool_executor.mark_tool_use_executed("")
    assert not tool_executor.is_tool_use_executed("")
    limit = tool_executor._EXECUTED_TOOL_USE_MAX
    for i in range(limit + 10):
        tool_executor.mark_tool_use_executed(f"toolu_{i}")
    assert len(tool_executor._EXECUTED_TOOL_USE_IDS) == limit
    assert not tool_executor.is_tool_use_executed("toolu_0")
    assert tool_executor.is_tool_use_executed(f"toolu_{limit + 9}")


def test_cli_event_mapping_marks_tool_use_provider_executed():
    mapped = model_selector._map_cli_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_E", "name": "mcp__aads-tools__todo_write", "input": {"a": 1}},
            {"type": "tool_use", "id": "toolu_F", "name": "ToolSearch", "input": {}},
        ]},
    })
    uses = [e for e in mapped if e["type"] == "tool_use"]
    assert len(uses) == 2
    assert all(u["provider_executed"] is True for u in uses)
    assert uses[0]["tool_name"] == "todo_write"
    assert uses[1]["tool_use_id"] == "toolu_F"


def test_tool_result_only_user_turn_detection():
    only = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}]
    assert model_selector._is_tool_result_only_user_turn(only)
    with_text = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "x"},
        {"type": "text", "text": "계속해"},
    ]}]
    assert not model_selector._is_tool_result_only_user_turn(with_text)
    assert not model_selector._is_tool_result_only_user_turn([{"role": "user", "content": "안녕"}])
    assert not model_selector._is_tool_result_only_user_turn(
        [{"role": "assistant", "content": [{"type": "tool_result"}]}]
    )
    assert not model_selector._is_tool_result_only_user_turn([])


@pytest.mark.parametrize("relay_fn", ["_stream_cli_relay", "_stream_codex_relay"])
def test_relay_does_not_start_turn_from_tool_result_only_message(relay_fn):
    messages = [
        {"role": "user", "content": "진행해"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "todo_write", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    boom = AsyncMock(side_effect=AssertionError("relay 를 호출하면 안 된다"))

    async def _collect():
        with patch.object(model_selector, "_byok_owner_context", boom), \
                patch.object(model_selector, "_stream_cli_relay_once", boom, create=True), \
                patch.object(model_selector, "_stream_codex_relay_once", boom, create=True):
            return [e async for e in getattr(model_selector, relay_fn)(
                "claude-opus-5-5", "sys", messages, session_id="11111111-1111-1111-1111-111111111111",
            )]

    events = _run(_collect())

    assert [e["type"] for e in events] == ["done"]
    assert events[0]["skipped"] == "tool_result_only_turn"
    boom.assert_not_called()
