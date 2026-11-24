"""B-1 1단계 — 도구 N회 도달 턴의 백그라운드 진행 안내 계약 테스트."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

from app.services import chat_service as cs

EXEC_ID = "11111111-1111-4111-8111-111111111111"


class _Conn:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError("db down")


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False

        return _Ctx()


class _Redis:
    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    async def publish_token(self, stream_id, text, idx, owner_epoch=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.published.append((text, idx))
        return f"{idx}-0"


class _Sink:
    def __init__(self, fail=False):
        self.items = []
        self.fail = fail

    async def __call__(self, item):
        if self.fail:
            raise RuntimeError("queue down")
        self.items.append(item)
        return True


def _run(state, conn, upto, start=1, redis=None, sink=None, pool=None):
    redis = redis or _Redis()
    sink = sink or _Sink()
    idx = [0]

    async def _go():
        async def _gp():
            return pool or _Pool(conn)

        with patch.object(cs, "get_pool", _gp), patch.object(cs, "_redis_stream", redis):
            for n in range(start, upto + 1):
                state["tool_count"] = n
                idx[0] = await cs._deliver_long_turn_notice(state, "sess-1234", idx[0], sink)

    asyncio.run(_go())
    return [i[0] for i in sink.items]


def _state():
    return {"tool_count": 0, "content": "", "tool_events": [], "execution_id": EXEC_ID}


def test_a_fifteen_emits_once_and_updates_once(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    conn = _Conn()
    events = _run(_state(), conn, 30)
    assert len(events) == 1
    assert len(conn.calls) == 1
    payload = json.loads(events[0][len("data: "):])
    assert payload["type"] == "progress"
    assert "도구 15회" in payload["message"]
    assert conn.calls[0][1] == (uuid.UUID(EXEC_ID), 15)


def test_b_fourteen_does_nothing(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    conn = _Conn()
    assert _run(_state(), conn, 14) == []
    assert conn.calls == []


def test_c_db_failure_does_not_propagate(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    conn = _Conn(fail=True)
    events = _run(_state(), conn, 20)
    assert len(events) == 1
    assert len(conn.calls) == 1


def test_d_env_threshold_and_invalid_fallback(monkeypatch):
    monkeypatch.setenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", "3")
    conn = _Conn()
    events = _run(_state(), conn, 5)
    assert len(events) == 1 and "도구 3회" in events[0]
    for bad in ("abc", "0", "-2", ""):
        monkeypatch.setenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", bad)
        assert cs._long_turn_notice_threshold() == 15


def test_e_notice_not_in_content_or_tool_events(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    state = _state()
    events = _run(state, _Conn(), 20)
    assert events
    assert state["content"] == ""
    assert state["tool_events"] == []


def test_f_sql_does_not_overwrite_tool_count_top_level():
    sql = cs._LONG_TURN_NOTICE_SQL
    assert "COALESCE(interruption_diagnostics,'{}'::jsonb) ||" in sql
    assert "jsonb_build_object('background_notice'" in sql
    # 최상위 tool_count 키를 만드는 jsonb_build_object 는 없어야 한다.
    assert "jsonb_build_object('tool_count'" not in sql
    assert "'tool_count', $2" in sql and sql.index("background_notice") < sql.index("'tool_count'")


def test_no_execution_id_skips_update(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    state = _state()
    state.pop("execution_id")
    conn = _Conn()
    assert len(_run(state, conn, 15)) == 1
    assert conn.calls == []


def test_g_real_send_path_publishes_and_queues_once(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    redis, sink = _Redis(), _Sink()
    _run(_state(), _Conn(), 30, redis=redis, sink=sink)
    assert len(redis.published) == 1 and len(sink.items) == 1
    assert json.loads(redis.published[0][0][len("data: "):])["type"] == "progress"
    assert sink.items[0][0] == redis.published[0][0]


def test_h_send_failure_does_not_propagate_and_flag_stays(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    state = _state()
    redis, sink = _Redis(fail=True), _Sink(fail=True)
    _run(state, _Conn(), 25, redis=redis, sink=sink)
    assert state["long_turn_notice_sent"] is True
    assert redis.published == [] and sink.items == []


def test_i_main_to_retry_path_switch_emits_once(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    state = _state()
    redis, sink = _Redis(), _Sink()
    _run(state, _Conn(), 16, redis=redis, sink=sink)
    _run(state, _Conn(), 25, start=17, redis=redis, sink=sink)
    assert len(sink.items) == 1 and len(redis.published) == 1


def test_j_slow_acquire_is_bounded_and_notice_already_sent(monkeypatch):
    monkeypatch.delenv("CHAT_LONG_TURN_NOTICE_TOOL_THRESHOLD", raising=False)
    monkeypatch.setenv("CHAT_LONG_TURN_NOTICE_DB_TIMEOUT_S", "0.05")

    class _SlowPool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_):
                    await asyncio.sleep(5)

                async def __aexit__(self_, *a):
                    return False

            return _Ctx()

    redis, sink = _Redis(), _Sink()
    state = _state()
    t0 = time.monotonic()
    _run(state, None, 15, start=15, redis=redis, sink=sink, pool=_SlowPool())
    assert time.monotonic() - t0 < 1.0
    assert len(sink.items) == 1 and len(redis.published) == 1
