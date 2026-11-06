"""AADS-TERMINATE-TASK-CANCELLED-STATUS-20261106.

terminate_task 강제 종료는 실패가 아니라 취소다 — pipeline_jobs 에 status='cancelled' 와
error_detail='terminated: …' 를 남겨 러너 실패 통계에서 빠지게 한다.
"""

import asyncio

import pytest


class _Conn:
    def __init__(self, row):
        self.row = row
        self.executed = []

    async def fetchrow(self, sql, *args):
        return self.row if "pipeline_jobs" in sql else None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _run(monkeypatch, row, reason="stall"):
    import app.core.db_pool as db_pool
    import app.services.pipeline_runner_service as prs
    import app.services.task_logger as tl
    from app.services import tool_executor as te

    conn = _Conn(row)
    completed = []
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))

    async def _noop(*a, **k):
        return None

    async def _completed(task_id, status, *a, **k):
        completed.append((task_id, status))

    monkeypatch.setattr(prs, "_reconcile_job_goal_links", _noop)
    monkeypatch.setattr(tl, "emit_task_log", _noop)
    monkeypatch.setattr(tl, "emit_task_completed", _completed)
    monkeypatch.setattr(te, "_local_pid_alive", lambda pid: False)

    async def _go():
        res = await te.ToolExecutor()._terminate_task({"task_id": row["job_id"], "reason": reason})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return res

    return asyncio.run(_go()), conn, completed


def _status_updates(conn):
    return [e for e in conn.executed if "UPDATE pipeline_jobs SET status=" in e[0]]


@pytest.mark.parametrize("project", ["GO100", "AADS"])
def test_running_job_is_cancelled_with_terminated_detail(monkeypatch, project):
    row = {"job_id": "runner-t0000001", "status": "running", "phase": "claude_code_work",
           "project": project, "runner_pid": None}
    _, conn, completed = _run(monkeypatch, row, reason="x" * 300)
    updates = _status_updates(conn)
    assert len(updates) == 1
    sql, args = updates[0]
    assert "status='cancelled'" in sql and "phase='cancelled'" in sql
    assert "status='error'" not in sql
    assert "error_detail=$3" in sql
    assert args[2] == "terminated: " + "x" * 200
    assert "강제종료" in args[1]  # review_feedback 덧붙이기는 유지
    assert completed == [("runner-t0000001", "cancelled")]


@pytest.mark.parametrize("status", ["done", "error", "cancelled"])
def test_finished_job_is_already_finished_without_update(monkeypatch, status):
    row = {"job_id": "runner-t0000002", "status": status, "phase": status,
           "project": "GO100", "runner_pid": None}
    res, conn, completed = _run(monkeypatch, row)
    assert res["result"] == "already_finished"
    assert _status_updates(conn) == []
    assert completed == []


def test_remote_runner_return_keys_unchanged(monkeypatch):
    row = {"job_id": "runner-t0000003", "status": "running", "phase": "claude_code_work",
           "project": "GO100", "runner_pid": 1234}
    res, _, _ = _run(monkeypatch, row)
    assert res["result"] == "db_marked_error"
    assert {"result", "pipeline", "project", "runner_pid"} <= set(res)
    assert res["runner_pid"] == 1234


def test_retry_and_cancel_accept_cancelled_status():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "app/services/pipeline_runner_service.py").read_text(encoding="utf-8")
    assert src.count('("error", "done", "cancelled")') == 2
    assert 'row["status"] == "cancelled" or' in src
