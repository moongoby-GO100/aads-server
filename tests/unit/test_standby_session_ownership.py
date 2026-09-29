"""컷오버 후 스탠바이 슬롯이 세션을 쥐고 다음 릴리스를 막는 교착 — 회귀 시험.

2026-09-30 #5290~#5292: green 이 스탠바이가 된 뒤에도 컷오버 전에 시작된 턴
(c7fee5ba, 14f1f6de)이 27~31분을 가며 target_slot_drain 을 막았다.

① 컷오버 후 새 내부 턴은 스탠바이에서 열리지 않고 active 로 넘어간다
② 진행 중인 턴은 건드리지 않는다(끊지 않는다)
③ 스탠바이 장기 점유가 deploy_standby_holds 에 기록된다
④ 승인 세션이 스탠바이를 쥐고 있으면 승인 응답에 경고가 붙는다(막지 않는다)
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import chat_service, standby_slot_ownership as sso

SESSION = "9102c970-905b-41c9-9de0-f8752f7a5833"
EXECUTION = "c7fee5ba-c89c-4442-9799-bc88dd15b792"


class _FakeConn:
    """SQL 을 기록하고, 조회에는 미리 정한 행을 돌려준다."""

    def __init__(self, standby_rows=None):
        self.standby_rows = list(standby_rows or [])
        self.executed: list[tuple[str, tuple]] = []
        self.fetched: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "UPDATE deploy_standby_holds" in sql:
            return "UPDATE 1"
        return "OK"

    async def fetch(self, sql, *args):
        self.fetched.append((sql, args))
        if "FROM chat_turn_executions te" in sql:
            session_filter = args[2]
            return [
                r for r in self.standby_rows
                if session_filter is None or r["session_id"] == str(session_filter)
            ]
        return []


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _hold_row(session_id=SESSION, execution_id=EXECUTION, owner="aads-server-green"):
    return {
        "execution_id": execution_id,
        "session_id": session_id,
        "owner_instance": owner,
        "status": "running",
        "started_at": datetime(2026, 9, 29, 16, 3, 36, tzinfo=timezone.utc),
        "heartbeat_at": datetime(2026, 9, 29, 16, 30, 0, tzinfo=timezone.utc),
        "held_seconds": 1600,
    }


@pytest.fixture
def active_is_blue(tmp_path, monkeypatch):
    marker = tmp_path / ".active_container"
    marker.write_text("aads-server\n", encoding="utf-8")
    monkeypatch.setenv("AADS_ACTIVE_CONTAINER_FILE", str(marker))
    return marker


# ── ① 컷오버 후 새 턴은 active 가 소유한다 ─────────────────────────────


async def test_standby_hands_internal_turn_to_active_instead_of_opening_it():
    enqueue = AsyncMock(return_value="deferred-1")
    with patch.object(chat_service, "_is_local_active_api_slot", return_value=False), \
            patch.object(chat_service, "_enqueue_deferred_reaction", enqueue):
        deferred = await chat_service.handoff_internal_turn_if_standby(
            SESSION, "[목표 진행] M3 착수", source="goal_dispatch",
        )
    assert deferred == "deferred-1"
    # 원문 그대로 넘긴다 — active 슬롯이 같은 지시로 턴을 연다.
    enqueue.assert_awaited_once_with(SESSION, "[목표 진행] M3 착수")


async def test_active_slot_keeps_running_internal_turn_locally():
    enqueue = AsyncMock()
    with patch.object(chat_service, "_is_local_active_api_slot", return_value=True), \
            patch.object(chat_service, "_enqueue_deferred_reaction", enqueue):
        assert await chat_service.handoff_internal_turn_if_standby(
            SESSION, "x", source="goal_dispatch",
        ) is None
    enqueue.assert_not_awaited()


async def test_goal_dispatch_on_standby_never_opens_a_stream():
    from app.services import goal_dispatch

    stream_calls = []

    async def _fake_stream(**kwargs):
        stream_calls.append(kwargs)
        yield "data: {}\n\n"

    with patch.object(chat_service, "_is_local_active_api_slot", return_value=False), \
            patch.object(chat_service, "_enqueue_deferred_reaction",
                         AsyncMock(return_value="deferred-2")) as enqueue, \
            patch.object(chat_service, "send_message_stream", _fake_stream), \
            patch.object(goal_dispatch, "_note_detached", AsyncMock()):
        await goal_dispatch._send_milestone(
            milestone_id=str(uuid.uuid4()),
            session_id=SESSION,
            message="[목표 진행 — 착수] 마일스톤",
            attempt=1,
            project="AADS",
        )
    assert stream_calls == []
    enqueue.assert_awaited_once()


async def test_session_relay_reply_on_standby_is_handed_off():
    from app.services import session_relay

    stream_calls = []

    async def _fake_stream(**kwargs):
        stream_calls.append(kwargs)
        yield "data: {}\n\n"

    with patch.object(session_relay, "_target_is_busy", AsyncMock(return_value=False)), \
            patch.object(session_relay, "_relay_paused", AsyncMock(return_value=False)), \
            patch.object(chat_service, "_is_local_active_api_slot", return_value=False), \
            patch.object(chat_service, "_enqueue_deferred_reaction",
                         AsyncMock(return_value="deferred-3")) as enqueue, \
            patch.object(chat_service, "send_message_stream", _fake_stream):
        delivered = await session_relay._deliver_answer(SESSION, "📨 답이 도착했습니다")
    assert delivered is True
    assert stream_calls == []
    enqueue.assert_awaited_once_with(SESSION, "📨 답이 도착했습니다")


def test_milestone_review_routes_through_standby_handoff():
    from app.services import milestone_review

    src = inspect.getsource(milestone_review)
    handoff = src.index("handoff_internal_turn_if_standby(")
    stream = src.index("cs.send_message_stream(")
    assert handoff < stream


async def test_deferred_queue_is_claimed_only_by_active_slot():
    # 넘긴 턴을 스탠바이가 다시 집으면 제자리다.
    with patch.object(chat_service, "_is_local_active_api_slot", return_value=False):
        assert await chat_service._process_deferred_reactions_once() == 0


def test_new_execution_owner_is_the_running_process():
    # owner_instance 결정 지점: INSERT 는 이 프로세스의 슬롯 이름을 쓴다.
    src = inspect.getsource(chat_service._get_or_create_turn_execution)
    assert "INSERT INTO chat_turn_executions" in src
    assert "_EXECUTION_OWNER_INSTANCE" in src


# ── ② 진행 중인 턴은 끊지 않는다 ───────────────────────────────────────


async def test_handoff_does_not_touch_running_executions():
    conn = _FakeConn()
    with patch.object(chat_service, "_is_local_active_api_slot", return_value=False), \
            patch.object(chat_service, "get_pool", return_value=_Pool(conn)):
        conn.fetchval = AsyncMock(side_effect=[None, "deferred-4"])
        deferred = await chat_service.handoff_internal_turn_if_standby(
            SESSION, "지시", source="test",
        )
    assert deferred == "deferred-4"
    assert all("chat_turn_executions" not in sql for sql, _ in conn.executed)


async def test_hold_recording_never_mutates_or_cancels_executions(active_is_blue):
    conn = _FakeConn([_hold_row()])
    await sso.record_standby_holds(conn, active_instance="aads-server")
    for sql, _ in conn.executed:
        assert "UPDATE chat_turn_executions" not in sql
        assert "DELETE" not in sql.upper()
    module_src = inspect.getsource(sso)
    assert "UPDATE chat_turn_executions" not in module_src
    assert "pg_cancel_backend" not in module_src
    assert "pg_terminate_backend" not in module_src


# ── ③ 스탠바이 장기 점유가 기록된다 ────────────────────────────────────


async def test_long_standby_hold_is_recorded(active_is_blue):
    sso._schema_ready = False
    conn = _FakeConn([_hold_row()])
    result = await sso.record_standby_holds(conn, active_instance="aads-server")

    assert result["open"] == 1
    assert result["holds"][0]["execution_id"] == EXECUTION
    # 임계(기본 600초)가 조회에 들어간다.
    standby_query = [a for s, a in conn.fetched if "FROM chat_turn_executions te" in s][0]
    assert standby_query[0] == "aads-server"
    assert standby_query[1] == sso.STANDBY_HOLD_THRESHOLD_SECONDS == 600
    upserts = [a for s, a in conn.executed if "INSERT INTO deploy_standby_holds" in s]
    assert upserts == [(EXECUTION, SESSION, "aads-server-green", "aads-server", 1600)]
    assert any("CREATE TABLE IF NOT EXISTS deploy_standby_holds" in s for s, _ in conn.executed)
    released = [a for s, a in conn.executed if "UPDATE deploy_standby_holds" in s]
    assert released == [([uuid.UUID(EXECUTION)],)]


async def test_unknown_active_slot_records_nothing():
    conn = _FakeConn([_hold_row()])
    result = await sso.record_standby_holds(conn, active_instance="")
    assert result["skipped"] == "active_instance_unknown"
    assert conn.executed == [] and conn.fetched == []


async def test_monitor_runs_only_on_active_slot():
    with patch.object(chat_service, "_is_local_active_api_slot", return_value=False):
        assert await sso.run_standby_hold_monitor_once() == {"skipped": "not_active_slot"}


def test_standby_query_only_counts_live_leases_off_the_active_slot():
    sql = sso._STANDBY_OWNED_SQL
    assert "te.owner_instance <> $1" in sql
    assert "te.lease_expires_at > NOW()" in sql
    assert "status IN ('running', 'retrying')" in sql


def test_ops_exposes_standby_holds_before_release():
    from app.api import ops

    paths = {getattr(r, "path", "") for r in ops.router.routes}
    assert "/ops/standby-holds" in paths


# ── ④ 승인 세션이 스탠바이를 쥐고 있으면 경고 ─────────────────────────


async def test_approval_warns_when_approving_session_holds_standby(active_is_blue):
    other = _hold_row(session_id=str(uuid.uuid4()), execution_id=str(uuid.uuid4()))
    conn = _FakeConn([_hold_row(), other])
    warning = await sso.approval_standby_warning(SESSION, conn=conn)

    assert warning is not None
    assert warning["code"] == "approving_session_holds_standby_slot"
    assert warning["active_instance"] == "aads-server"
    assert [r["execution_id"] for r in warning["approving_session_executions"]] == [EXECUTION]
    assert warning["other_sessions_on_standby"] == 1
    assert "aads-server-green" in warning["message"]
    json.dumps(warning, ensure_ascii=False)  # 도구 응답에 그대로 실린다


async def test_no_warning_when_session_runs_on_active(active_is_blue):
    conn = _FakeConn([])
    assert await sso.approval_standby_warning(SESSION, conn=conn) is None


async def test_warning_lookup_failure_never_breaks_approval(active_is_blue):
    class _Broken(_FakeConn):
        async def fetch(self, sql, *args):
            raise RuntimeError("db down")

    assert await sso.approval_standby_warning(SESSION, conn=_Broken()) is None


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Resp(self.payload)


async def test_tool_executor_approve_carries_warning_without_blocking():
    from app.services import tool_executor as te

    client = _Client({"status": "approved", "job_id": "runner-a5f21866"})
    warning = {"code": "approving_session_holds_standby_slot", "message": "⚠️"}
    token = te.current_chat_session_id.set(SESSION)
    try:
        with patch("httpx.AsyncClient", return_value=client), \
                patch.object(sso, "approval_standby_warning",
                             AsyncMock(return_value=warning)) as warn:
            result = await te.ToolExecutor()._pipeline_runner_approve(
                {"job_id": "runner-a5f21866", "action": "approve"}
            )
    finally:
        te.current_chat_session_id.reset(token)

    assert len(client.posts) == 1  # 승인은 그대로 나갔다
    assert result["status"] == "approved"
    assert result["standby_slot_warning"] == warning
    warn.assert_awaited_once_with(SESSION)


async def test_tool_executor_reject_skips_warning():
    from app.services import tool_executor as te

    client = _Client({"status": "rejected"})
    with patch("httpx.AsyncClient", return_value=client), \
            patch.object(sso, "approval_standby_warning", AsyncMock()) as warn:
        result = await te.ToolExecutor()._pipeline_runner_approve(
            {"job_id": "runner-x", "action": "reject"}
        )
    assert "standby_slot_warning" not in result
    warn.assert_not_awaited()


async def test_ceo_chat_tools_approve_carries_warning():
    from app.api import ceo_chat_tools

    client = _Client({"status": "approved"})
    warning = {"code": "approving_session_holds_standby_slot", "message": "⚠️ 스탠바이"}
    with patch("httpx.AsyncClient", return_value=client), \
            patch.object(sso, "approval_standby_warning",
                         AsyncMock(return_value=warning)) as warn:
        text = await ceo_chat_tools.execute_tool(
            "pipeline_runner_approve", {"job_id": "runner-a5f21866"}, dsn="",
            chat_session_id=SESSION,
        )
    body = json.loads(text)
    assert body["status"] == "approved"
    assert body["standby_slot_warning"]["code"] == "approving_session_holds_standby_slot"
    warn.assert_awaited_once_with(SESSION)
