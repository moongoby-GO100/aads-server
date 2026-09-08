"""A transport retry must keep its assistant row writable by the producer."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import chat_service as svc


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["llm_retry_error", "llm_retry_exception", "missing_done_event_stream_closed:attempt=1"])
async def test_retry_preserves_same_live_bubble_without_terminal_marker(reason):
    sid, eid, mid = map(str, (uuid4(), uuid4(), uuid4()))
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": mid, "created_at_text": "2026-09-08T00:00:00Z"}
    state = {"execution_id": eid, "owner_epoch": 7, "completed": False}
    with patch.object(svc, "get_pool", return_value=Pool(conn)), patch.dict(svc._streaming_state, {sid: state}):
        saved = await svc._save_interrupted_partial_message(
            sid, "검증 중인 부분 응답", reason=reason, execution_id=eid, continuing=True
        )
    assert saved["id"] == mid
    assert saved["content"] == "검증 중인 부분 응답"
    assert saved["intent"] == "streaming_placeholder"
    assert saved["model_used"] == "streaming"
    assert not state["completed"]
    sql, *args = conn.fetchrow.call_args.args
    assert "FOR UPDATE" in sql and "owner_epoch = $4" in sql
    assert "lease_expires_at > NOW()" in sql
    assert args[3] == 7
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_retry_rejects_lost_lease_without_terminal_fallback():
    sid, eid = map(str, (uuid4(), uuid4()))
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with patch.object(svc, "get_pool", return_value=Pool(conn)), patch.dict(
        svc._streaming_state, {sid: {"execution_id": eid, "owner_epoch": 2}}
    ):
        with pytest.raises(RuntimeError, match="fence_rejected"):
            await svc._save_interrupted_partial_message(
                sid, "부분 응답", reason="llm_retry_error", execution_id=eid, continuing=True
            )
    assert conn.fetchrow.await_count == 1
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_old_producer_cannot_save_into_new_execution():
    sid, old_eid, new_eid = map(str, (uuid4(), uuid4(), uuid4()))
    with patch.object(svc, "get_pool") as pool, patch.dict(
        svc._streaming_state, {sid: {"execution_id": new_eid, "owner_epoch": 3}}
    ):
        with pytest.raises(RuntimeError, match="owner_missing"):
            await svc._save_interrupted_partial_message(
                sid, "이전 응답", reason="llm_retry_error", execution_id=old_eid, continuing=True
            )
    pool.assert_not_called()


@pytest.mark.parametrize("content", [
    "원인은 응답 생성이 중단된 뒤 재시도 상태를 잘못 처리한 것입니다.",
    "최신 지시를 우선 처리하는 코드에서 잘못 중단 처리되었습니다.",
    "부분 응답\n\n" + svc._INTERRUPT_MARKER,
])
def test_status_poll_never_cancels_live_producer_from_response_text(content):
    sid = str(uuid4())
    task = MagicMock()
    task.done.return_value = False
    state = {"execution_id": str(uuid4()), "content": content,
             "started_at": svc._bg_time.monotonic(), "completed": False}
    with patch.dict(svc._streaming_state, {sid: state}), patch.dict(svc._active_bg_tasks, {sid: task}):
        status = svc.get_streaming_status(sid)
    assert status["is_streaming"] is True
    assert status["just_completed"] is False
    assert state["completed"] is False
    task.cancel.assert_not_called()
