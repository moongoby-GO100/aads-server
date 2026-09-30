"""배포로 끊긴 running 작업의 재큐잉 판별 (AADS-RUNNER-ORPHAN-REQUEUE-20260930).

recover_interrupted_jobs() 의 Phase 0 이 fake conn 으로 어떤 SQL·메시지를 내는지 검증한다.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import app.core.db_pool as db_pool
from app.services import pipeline_runner_service as svc


class _FakeConn:
    def __init__(self, requeue_rows=None, orphan_rows=None):
        self.requeue_rows = requeue_rows or []
        self.orphan_rows = orphan_rows or []
        self.fetch_sql: list[str] = []
        self.fetch_args: list[tuple] = []
        self.exec_sql: list[str] = []
        self.exec_args: list[tuple] = []

    async def fetch(self, sql, *args):
        self.fetch_sql.append(sql)
        self.fetch_args.append(args)
        if "SET status = 'queued'" in sql:
            return self.requeue_rows
        if "requeue_exhausted" in sql:
            return self.orphan_rows
        return []

    async def execute(self, sql, *args):
        self.exec_sql.append(sql)
        self.exec_args.append(args)
        if "SET status = 'error', phase = 'error'" in sql:
            return f"UPDATE {len(self.orphan_rows)}"
        return "UPDATE 0"

    async def fetchval(self, sql, *args):
        return 0


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _run(conn, monkeypatch, env=None):
    for key in ("AADS_ORPHAN_REQUEUE_ENABLED", "AADS_ORPHAN_REQUEUE_MAX"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(db_pool, "get_pool", lambda: _FakePool(conn))

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(svc, "_reconcile_job_goal_links", _noop)
    try:
        asyncio.run(svc.recover_interrupted_jobs())
    except Exception:
        # recover_interrupted_jobs 는 내부에서 예외를 삼키지만, Phase 0 이후 단계를 fake conn 이
        # 다 흉내내지는 못한다. Phase 0 결과만 본다.
        pass


def _requeue_sql(conn):
    return next((s for s in conn.fetch_sql if "SET status = 'queued'" in s), None)


def _chat_inserts(conn):
    return [a[1] for s, a in zip(conn.exec_sql, conn.exec_args) if "INSERT INTO chat_messages" in s]


REQUEUED = {
    "job_id": "runner-aaa11111",
    "chat_session_id": "11111111-1111-1111-1111-111111111111",
    "project": "AADS",
    "runner_host": "contabo14",
    "instr": "TASK_ID: X",
    "requeue_no": 1,
}


def test_requeue_sql_only_targets_pre_commit_runner_jobs_and_keeps_liveness_guard(monkeypatch):
    conn = _FakeConn()
    _run(conn, monkeypatch)
    sql = _requeue_sql(conn)
    assert sql is not None
    assert "phase = 'claude_code_work'" in sql
    assert "commit_hash IS NULL" in sql
    assert "runner_host IS NOT NULL" in sql
    assert conn.fetch_args[0] == (2,)
    # 재큐잉·조회·error 확정 세 쿼리 모두 살아 있는 원격 러너 제외 조건을 유지해야 한다.
    error_update = next(s for s in conn.exec_sql if "SET status = 'error', phase = 'error'" in s)
    orphan_select = next(s for s in conn.fetch_sql if "requeue_exhausted" in s)
    for text in (sql, error_update, orphan_select):
        assert "FROM pipeline_runner_hosts h" in text
        assert "h.last_seen_at > now() - interval '15 minutes'" in text


def test_requeue_path_notifies_chat_as_recovery_not_failure(monkeypatch):
    conn = _FakeConn(requeue_rows=[dict(REQUEUED)])
    _run(conn, monkeypatch)
    messages = _chat_inserts(conn)
    assert len(messages) == 1
    text = messages[0]
    assert "자동 재큐잉" in text and "1/2" in text and REQUEUED["job_id"] in text
    for bad in ("⚠️", "중단", "실패", "재실행이 필요하면"):
        assert bad not in text
    assert any("pg_notify('pipeline_new_job'" in s for s in conn.exec_sql)
    assert any("INSERT INTO pipeline_runner_events" in s for s in conn.exec_sql)


def test_exhausted_path_marks_distinct_error_detail_and_reports_exhaustion(monkeypatch):
    conn = _FakeConn(orphan_rows=[{
        "job_id": "runner-bbb22222",
        "chat_session_id": "22222222-2222-2222-2222-222222222222",
        "project": "AADS",
        "instr": "TASK_ID: Y",
        "requeue_exhausted": True,
    }])
    _run(conn, monkeypatch)
    update_idx = next(i for i, s in enumerate(conn.exec_sql) if "SET status = 'error', phase = 'error'" in s)
    update_sql = conn.exec_sql[update_idx]
    assert "server_restart_orphan_requeue_exhausted" in update_sql
    assert "ELSE 'server_restart_orphan'" in update_sql
    assert conn.exec_args[update_idx] == (2,)
    messages = _chat_inserts(conn)
    assert len(messages) == 1
    assert "자동 재큐잉 2회 소진" in messages[0]


def test_non_exhausted_orphan_keeps_legacy_reason_and_message(monkeypatch):
    conn = _FakeConn(orphan_rows=[{
        "job_id": "runner-ccc33333",
        "chat_session_id": "33333333-3333-3333-3333-333333333333",
        "project": "AADS",
        "instr": "TASK_ID: Z",
        "requeue_exhausted": False,
    }])
    _run(conn, monkeypatch)
    messages = _chat_inserts(conn)
    assert len(messages) == 1
    assert "사유: 서버 재시작으로 중단됨" in messages[0]
    assert "소진" not in messages[0]


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no"])
def test_env_switch_disables_requeue(monkeypatch, value):
    conn = _FakeConn(requeue_rows=[dict(REQUEUED)])
    _run(conn, monkeypatch, {"AADS_ORPHAN_REQUEUE_ENABLED": value})
    assert _requeue_sql(conn) is None
    assert conn.fetch_args[0] == (0,)
    assert not any("자동 재큐잉" in m for m in _chat_inserts(conn))


def test_max_env_and_invalid_value(monkeypatch):
    monkeypatch.delenv("AADS_ORPHAN_REQUEUE_ENABLED", raising=False)
    monkeypatch.setenv("AADS_ORPHAN_REQUEUE_MAX", "5")
    assert svc._orphan_requeue_max() == 5
    monkeypatch.setenv("AADS_ORPHAN_REQUEUE_MAX", "abc")
    assert svc._orphan_requeue_max() == 2
    monkeypatch.delenv("AADS_ORPHAN_REQUEUE_MAX")
    assert svc._orphan_requeue_max() == 2


def test_requeue_marker_avoids_phase_0a_auto_resume_pattern():
    # Phase 0a 는 review_feedback LIKE '%서버 재시작으로 중단%' 인 error 작업을 새 job 으로 되살린다.
    assert "서버 재시작으로 중단" not in svc._ORPHAN_REQUEUE_MARK
    src = open(svc.__file__, encoding="utf-8").read()
    start = src.index("_ORPHAN_REQUEUE_MARK} 배포로")
    assert "서버 재시작으로 중단" not in src[start:start + 80]
