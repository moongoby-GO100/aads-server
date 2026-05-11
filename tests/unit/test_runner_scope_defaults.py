from __future__ import annotations

import json
from unittest.mock import patch

import pytest


_SESSION_ID = "11111111-1111-1111-1111-111111111111"


class _FakeResponse:
    def __init__(self, *, text: str = "[]", payload=None) -> None:
        self.text = text
        self._payload = [] if payload is None else payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *, headers=None, params=None, timeout=None):
        self.calls.append({
            "url": url,
            "headers": headers,
            "params": params,
            "timeout": timeout,
        })
        return self._response


class _FakeAcquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args):
        self.calls.append((query, args))
        if "FROM pipeline_jobs" in query:
            return [{
                "task_id": "runner-abc12345",
                "project": "AADS",
                "title": "current-session task",
                "pipeline": "pipeline_c",
                "phase": "running",
                "status": "running",
            }]
        if "FROM directive_lifecycle" in query:
            return [{
                "task_id": "42",
                "project": "AADS",
                "title": "pipeline-b task",
                "pipeline": "pipeline_b",
                "phase": "in_progress",
                "status": "in_progress",
            }]
        raise AssertionError(f"unexpected query: {query}")


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        return _FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_ceo_pipeline_runner_status_defaults_to_current_session():
    from app.api.ceo_chat_tools import execute_tool

    client = _FakeAsyncClient(_FakeResponse())

    with patch("httpx.AsyncClient", return_value=client):
        result = await execute_tool(
            "pipeline_runner_status",
            {},
            dsn="postgresql://unused",
            chat_session_id=_SESSION_ID,
        )

    assert json.loads(result) == []
    assert client.calls[0]["params"]["session_id"] == _SESSION_ID
    assert client.calls[0]["params"]["limit"] == "10"


@pytest.mark.asyncio
async def test_ceo_pipeline_runner_status_scope_all_skips_session_filter():
    from app.api.ceo_chat_tools import execute_tool

    client = _FakeAsyncClient(_FakeResponse())

    with patch("httpx.AsyncClient", return_value=client):
        await execute_tool(
            "pipeline_runner_status",
            {"scope": "all", "status": "queued"},
            dsn="postgresql://unused",
            chat_session_id=_SESSION_ID,
        )

    assert client.calls[0]["params"] == {"limit": "10", "status": "queued"}


@pytest.mark.asyncio
async def test_tool_executor_check_task_status_defaults_to_current_session():
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    fake_pool = _FakePool()
    token = current_chat_session_id.set(_SESSION_ID)
    try:
        with patch("app.core.db_pool.get_pool", return_value=fake_pool):
            result = await ToolExecutor()._check_task_status({})
    finally:
        current_chat_session_id.reset(token)

    assert result["scope"] == "current_session"
    assert result["session_id"] == _SESSION_ID
    assert result["pipeline_b_included"] is False
    assert len(result["tasks"]) == 1
    assert any("chat_session_id = $1" in query for query, _args in fake_pool.conn.calls)
    assert not any("directive_lifecycle" in query for query, _args in fake_pool.conn.calls)


@pytest.mark.asyncio
async def test_tool_executor_pipeline_runner_status_scope_all_skips_session_filter():
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    client = _FakeAsyncClient(_FakeResponse(payload=[]))
    token = current_chat_session_id.set(_SESSION_ID)
    try:
        with patch("httpx.AsyncClient", return_value=client):
            result = await ToolExecutor()._pipeline_runner_status({"scope": "all", "status": "queued"})
    finally:
        current_chat_session_id.reset(token)

    assert result == []
    assert client.calls[0]["params"] == {"limit": "10", "status": "queued"}
