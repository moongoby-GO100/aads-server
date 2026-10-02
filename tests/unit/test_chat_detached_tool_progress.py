"""브라우저 이탈 후 본문 변화 없는 도구 진행이 DB 에 저장되는지 (2026-10-03 회귀).

with_background_completion 을 실제로 돌리고 asyncpg 연결만 흉내 낸다. 저장 호출 횟수가 아니라
fixture 가 들고 있는 placeholder 행(content/tools_called)과 lease heartbeat 호출을 검증한다.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import chat_service


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        import time

        return getattr(time, name)


class _FakeDb:
    """chat_turn_executions 한 행과 streaming_placeholder 한 행을 가진 최소 DB."""

    def __init__(self, execution_id: uuid.UUID) -> None:
        self.execution_id = execution_id
        self.exec_status = "running"
        self.placeholder: dict | None = None
        self.insert_count = 0
        self.message_id = uuid.uuid4()

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def fetchrow(self, sql, *args):
        if "FROM chat_turn_executions" in sql and "SELECT status" in sql:
            return {
                "status": self.exec_status,
                "completed_at": None if self.exec_status in ("running", "retrying") else object(),
                "owner_instance": chat_service._EXECUTION_OWNER_INSTANCE,
                "owner_epoch": 1,
                "lease_expires_at": object(),
            }
        if "INSERT INTO chat_messages" in sql:
            self.insert_count += 1
            is_new = self.placeholder is None
            self.placeholder = {"content": args[2], "tools_called": json.loads(args[3])}
            return {"id": self.message_id, "is_new": is_new}
        return None

    async def execute(self, sql, *args):
        return "OK"

    def transaction(self):
        return _Acquire(self)


class _Acquire:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@pytest.mark.asyncio
async def test_detached_tool_only_progress_is_saved_and_unchanged_only_heartbeats():
    session_id = str(uuid.uuid4())
    execution_id = uuid.uuid4()
    db = _FakeDb(execution_id)
    clock = _Clock()
    heartbeat = AsyncMock(return_value=True)
    snapshots: dict[str, dict] = {}
    detached = asyncio.Event()

    async def gen():
        yield _sse({"type": "stream_start", "execution_id": str(execution_id)})
        snapshots["stream_start"] = {"inserts": db.insert_count, "hb": heartbeat.await_count}
        await detached.wait()

        # 브라우저 이탈 후: 본문(delta)은 한 글자도 없고 도구만 진행된다.
        clock.now += 6
        yield _sse({"type": "tool_use", "tool_name": "run_remote_command", "tool_input": {"command": "ls"}})
        snapshots["tool_use"] = {
            "inserts": db.insert_count,
            "hb": heartbeat.await_count,
            "placeholder": dict(db.placeholder or {}),
        }

        clock.now += 6
        yield _sse({"type": "tool_result", "tool_name": "run_remote_command", "content": "ok"})
        snapshots["tool_result"] = {
            "inserts": db.insert_count,
            "hb": heartbeat.await_count,
            "placeholder": dict(db.placeholder or {}),
        }

        # 변화 없는 구간: DB write 는 없어야 하고 lease heartbeat 기회는 유지되어야 한다.
        clock.now += 6
        yield _sse({"type": "heartbeat"})
        snapshots["unchanged"] = {"inserts": db.insert_count, "hb": heartbeat.await_count}

        # 실행이 terminal 로 닫히면 더 이상 저장하지 않는다.
        db.exec_status = "completed"
        clock.now += 6
        yield _sse({"type": "tool_use", "tool_name": "read_remote_file", "tool_input": {}})
        snapshots["terminal"] = {"inserts": db.insert_count}
        yield _sse({"type": "tool_result", "tool_name": "read_remote_file", "content": "x"})

    wrapper = chat_service.with_background_completion(gen(), session_id)
    redis_stub = AsyncMock()
    redis_stub.publish_token = AsyncMock(return_value=None)
    redis_stub.delete_stream = AsyncMock()
    redis_stub.mark_stream_done = AsyncMock()

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(db)),
        patch("app.services.chat_service._bg_time", clock),
        patch("app.services.chat_service._heartbeat_execution_lease", new=heartbeat),
        patch("app.services.chat_service._archive_competing_stream_placeholder", new=AsyncMock()),
        patch("app.services.chat_service._mark_execution_interrupted", new=AsyncMock()),
        patch("app.services.chat_service._retry_background_finalize_step", new=AsyncMock()),
        patch("app.services.chat_service._redis_stream", redis_stub),
    ):
        # stream_start 까지만 받고 브라우저를 끊는다.
        seen_stream_start = False
        while not seen_stream_start:
            item = await asyncio.wait_for(wrapper.__anext__(), timeout=5)
            seen_stream_start = "stream_start" in item
        task = chat_service._active_bg_tasks[session_id]
        await wrapper.aclose()
        await asyncio.sleep(0.05)
        detached.set()
        await asyncio.wait_for(task, timeout=10)

    state = chat_service._streaming_state.get(session_id) or {}
    try:
        # n1: stream_start 직후 저장 (도구 0회)
        assert snapshots["stream_start"]["inserts"] == 1

        # 핵심 회귀: 본문 0자 + 도구 호출 증가 → 저장된다.
        assert snapshots["tool_use"]["inserts"] == 2
        assert "도구 1회 호출" in snapshots["tool_use"]["placeholder"]["content"]
        assert any(e.get("type") == "tool_use" for e in snapshots["tool_use"]["placeholder"]["tools_called"])

        assert snapshots["tool_result"]["inserts"] == 3
        assert [e.get("type") for e in snapshots["tool_result"]["placeholder"]["tools_called"]] == [
            "tool_use",
            "tool_result",
        ]

        # 변화 없음: 중복 DB 쓰기 없이 lease heartbeat 만 갱신
        assert snapshots["unchanged"]["inserts"] == 3
        assert snapshots["unchanged"]["hb"] > snapshots["tool_result"]["hb"]

        # terminal: 저장 없음, producer 는 더 진행하지 않음
        assert "terminal" not in snapshots
        assert db.insert_count == 3
        assert state.get("_terminal_execution_closed") is True
    finally:
        chat_service._streaming_state.pop(session_id, None)
        chat_service._active_bg_tasks.pop(session_id, None)


@pytest.mark.asyncio
async def test_after_disconnect_save_skips_completed_state():
    session_id = str(uuid.uuid4())
    save = AsyncMock()

    async def gen():
        yield _sse({"type": "stream_start", "execution_id": str(uuid.uuid4())})
        await asyncio.sleep(0)

    wrapper = chat_service.with_background_completion(gen(), session_id)
    redis_stub = AsyncMock()
    redis_stub.publish_token = AsyncMock(return_value=None)
    redis_stub.delete_stream = AsyncMock()
    redis_stub.mark_stream_done = AsyncMock()
    db = _FakeDb(uuid.uuid4())

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(db)),
        patch("app.services.chat_service._interim_save_streaming", new=save),
        patch("app.services.chat_service._mark_execution_interrupted", new=AsyncMock()),
        patch("app.services.chat_service._retry_background_finalize_step", new=AsyncMock()),
        patch("app.services.chat_service._redis_stream", redis_stub),
    ):
        item = ""
        while "stream_start" not in item:
            item = await asyncio.wait_for(wrapper.__anext__(), timeout=5)
        state = chat_service._streaming_state[session_id]
        state["completed"] = True
        save.reset_mock()
        await wrapper.aclose()
        await asyncio.sleep(0.05)
        task = chat_service._active_bg_tasks.get(session_id)
        if task:
            await asyncio.wait_for(task, timeout=10)

    save.assert_not_awaited()
    chat_service._streaming_state.pop(session_id, None)
    chat_service._active_bg_tasks.pop(session_id, None)
