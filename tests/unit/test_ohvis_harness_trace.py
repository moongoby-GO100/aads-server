"""ohvis_harness_trace 단위 테스트.

핵심 계약: trace 기록은 절대 호출부를 깨뜨리지 않는다.
- 풀 미초기화 / insert 실패 / 테이블 부재 → 예외 없이 False
- 테이블이 있으면 ohvis_harness_traces 컬럼에 맞는 파라미터로 INSERT
"""
import asyncio

import pytest

from app.services import ohvis_harness_trace as trace_module
from app.services.ohvis_harness_trace import (
    TRACE_TABLE,
    record_goal_trace,
    record_trace,
    reset_table_cache,
)


class FakeConn:
    def __init__(self, table_exists: bool = True, raise_on_execute: bool = False):
        self.table_exists = table_exists
        self.raise_on_execute = raise_on_execute
        self.executed: list[tuple] = []

    async def fetchval(self, query: str, *args):
        return self.table_exists

    async def execute(self, query: str, *args):
        if self.raise_on_execute:
            raise RuntimeError("insert boom")
        self.executed.append((query, args))
        return "INSERT 0 1"


class FakeAcquire:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc_info):
        return False


class FakePool:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_table_cache()
    yield
    reset_table_cache()


def _patch_pool(monkeypatch, conn: FakeConn | None, error: Exception | None = None) -> None:
    import app.core.db_pool as db_pool

    def _get_pool():
        if error is not None:
            raise error
        return FakePool(conn)

    monkeypatch.setattr(db_pool, "get_pool", _get_pool)


def test_record_trace_inserts_row_when_table_exists(monkeypatch) -> None:
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    ok = asyncio.run(
        record_trace(
            graph_run_id="goal:abc",
            project="AADS",
            run_type="goal_advance",
            input_summary="advance goal abc",
            output_summary="advanced=True",
            metadata={"component": "goal_control_loop"},
            tool_calls=[{"name": "advance_goal"}],
        )
    )

    assert ok is True
    assert len(conn.executed) == 1
    query, args = conn.executed[0]
    assert f"INSERT INTO {TRACE_TABLE}" in query
    assert args[0] == "goal:abc"
    assert args[1] == "AADS"
    assert args[7] == "goal_advance"
    assert '"component": "goal_control_loop"' in args[13]


def test_record_trace_skips_when_table_missing(monkeypatch) -> None:
    conn = FakeConn(table_exists=False)
    _patch_pool(monkeypatch, conn)

    first = asyncio.run(record_trace(graph_run_id="goal:x", run_type="goal_create"))
    second = asyncio.run(record_trace(graph_run_id="goal:y", run_type="goal_create"))

    assert first is False
    assert second is False
    assert conn.executed == []


def test_record_trace_never_raises_on_pool_or_insert_failure(monkeypatch) -> None:
    _patch_pool(monkeypatch, None, error=RuntimeError("DB pool이 초기화되지 않았습니다"))
    assert asyncio.run(record_trace(graph_run_id="goal:x")) is False

    reset_table_cache()
    _patch_pool(monkeypatch, FakeConn(table_exists=True, raise_on_execute=True))
    assert asyncio.run(record_trace(graph_run_id="goal:x")) is False


def test_record_trace_requires_graph_run_id(monkeypatch) -> None:
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    assert asyncio.run(record_trace(graph_run_id="")) is False
    assert conn.executed == []


def test_record_trace_sanitizes_ids_and_clips_summaries(monkeypatch) -> None:
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    asyncio.run(
        record_trace(
            graph_run_id="goal:clip",
            session_id="not-a-uuid",
            ohvis_task_id="0f3a2f2a-5f1b-4a0e-9a3d-2f7a1b6c9d10",
            input_summary="가" * 5000,
            error="e" * 5000,
            metadata={"unserializable": object()},
        )
    )

    _query, args = conn.executed[0]
    assert args[2] is None, "잘못된 session_id는 NULL로 저장되어야 한다"
    assert args[3] == "0f3a2f2a-5f1b-4a0e-9a3d-2f7a1b6c9d10"
    assert len(args[8]) <= trace_module.SUMMARY_LIMIT
    assert len(args[12]) <= trace_module.ERROR_LIMIT
    assert args[13].startswith("{")


def test_record_trace_reuses_caller_connection_without_touching_pool(monkeypatch) -> None:
    """호출부가 커넥션을 점유한 채 남기는 trace는 중첩 acquire를 하지 않는다."""
    caller_conn = FakeConn(table_exists=True)

    def _boom():
        raise AssertionError("conn이 주어지면 pool을 acquire하면 안 된다")

    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", _boom)

    ok = asyncio.run(
        record_trace(
            graph_run_id="goal:held",
            run_type="goal_activate",
            project="AADS",
            conn=caller_conn,
        )
    )

    assert ok is True
    assert len(caller_conn.executed) == 1
    assert caller_conn.executed[0][1][7] == "goal_activate"


def test_record_trace_with_caller_connection_is_still_non_fatal(monkeypatch) -> None:
    caller_conn = FakeConn(table_exists=True, raise_on_execute=True)
    _patch_pool(monkeypatch, None, error=RuntimeError("pool 미초기화"))

    assert asyncio.run(record_trace(graph_run_id="goal:held", conn=caller_conn)) is False


def test_record_goal_trace_builds_goal_scoped_run_id(monkeypatch) -> None:
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    ok = asyncio.run(
        record_goal_trace(
            "milestone_add",
            goal_id="0f3a2f2a-5f1b-4a0e-9a3d-2f7a1b6c9d10",
            project="AADS",
            milestone_id="11111111-2222-3333-4444-555555555555",
            outcome="status=pending",
        )
    )

    assert ok is True
    _query, args = conn.executed[0]
    assert args[0] == "goal:0f3a2f2a-5f1b-4a0e-9a3d-2f7a1b6c9d10"
    assert "goal.milestone_add" in args[8]
    assert args[9] == "status=pending"
    assert '"component": "goal_control_loop"' in args[13]
