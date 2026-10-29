"""Automatic notes must contain validated summaries, never question fallbacks."""

import json
import logging
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from app.services import memory_manager

QUESTION = "[시스템] 질문 원문을 요약 대신 저장해서는 안 됩니다. " * 10
SUMMARY = "자동 요약 응답 검증과 실패 시 저장 생략을 결정했다."
MESSAGES = [{"role": "user", "content": QUESTION}]


@pytest.fixture
def llm(monkeypatch):
    # Only the central client's call boundary is mocked; no provider is invoked.
    client = ModuleType("app.core.anthropic_client")
    client.call_llm_with_fallback = AsyncMock()
    monkeypatch.setitem(sys.modules, client.__name__, client)
    return client.call_llm_with_fallback


@pytest.fixture
def connection(monkeypatch):
    conn = AsyncMock()
    conn.fetchrow.side_effect = lambda query, *args: {
        "id": 7, "session_id": args[0], "summary": args[1],
        "content": args[1], "key_decisions": args[2], "action_items": args[3],
        "unresolved_issues": args[4], "projects_discussed": args[5],
    }
    get_conn = AsyncMock(return_value=conn)
    monkeypatch.setattr(memory_manager, "_get_conn", get_conn)
    return conn, get_conn


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", [
    "{}", "```json\n{}\n```", "요약 결과입니다:\n{}\n설명 끝.",
    "설명\n```json\n{}\n```\n끝",
])
async def test_valid_json_is_saved(llm, connection, wrapper):
    llm.return_value = wrapper.format(json.dumps({
        "summary": SUMMARY, "key_decisions": ['문자열 안의 {중괄호}'],
    }, ensure_ascii=False))
    manager = memory_manager.MemoryManager()
    note = await manager.save_session_note("test-session", MESSAGES)
    assert note.summary == SUMMARY
    assert note.summary != QUESTION[:100]
    assert note.key_decisions == ['문자열 안의 {중괄호}']
    connection[0].fetchrow.assert_awaited_once()
    connection[0].close.assert_awaited_once()
    assert manager.auto_summarize_failures == 0
    assert llm.call_args.kwargs["model"] == "qwen-turbo"
    assert llm.call_args.kwargs["max_tokens"] == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    '{"summary":', "JSON이 없는 응답", '{"summary": ""}',
    '{"summary": "   "}', '{"summary": "짧음"}', '{}',
    '{"summary": null}', '{"summary": 123}', None,
    json.dumps({"summary": QUESTION[:100]}),
    json.dumps({"summary": SUMMARY, "action_items": "invalid"}),
    RuntimeError("private provider details"),
])
async def test_failed_summary_never_opens_db(llm, connection, caplog, raw):
    if isinstance(raw, Exception):
        llm.side_effect = raw
    else:
        llm.return_value = raw
    manager = memory_manager.MemoryManager()
    with caplog.at_level(logging.WARNING, logger=memory_manager.__name__):
        for _ in range(2):
            note = await manager.save_session_note("test-session", MESSAGES)
            assert note.id == 0
            assert note.summary == ""
            assert note.summary != QUESTION[:100]
    connection[1].assert_not_awaited()
    connection[0].fetchrow.assert_not_awaited()
    assert manager.auto_summarize_failures == 2
    assert "auto_summarize failed" in caplog.text
    assert "failures=2" in caplog.text
    assert QUESTION[:30] not in caplog.text
    assert "private provider details" not in caplog.text


@pytest.mark.asyncio
async def test_empty_messages_skip_llm_and_db(llm, connection, caplog):
    note = await memory_manager.MemoryManager().save_session_note("empty", [])
    assert note.summary == ""
    llm.assert_not_awaited()
    connection[1].assert_not_awaited()
    assert "auto_summarize failed" in caplog.text


@pytest.mark.asyncio
async def test_explicit_summary_preserved(llm, connection):
    note = await memory_manager.MemoryManager().save_session_note(
        "manual", MESSAGES, summary="수동 요약", key_decisions=["유지"],
    )
    assert note.summary == "수동 요약"
    assert note.key_decisions == ["유지"]
    llm.assert_not_awaited()
