from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


class _FakeConn:
    def __init__(self, *, fetchrow_result=None) -> None:
        self.fetchrow_result = fetchrow_result
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_result

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "INSERT 0 1"


def test_runner_display_status_classifies_terminal_non_error_states():
    from app.api.pipeline_runner import _runner_display_status

    assert _runner_display_status("cancelled", "no_changes", "no_changes") == {
        "display_status": "no_changes",
        "status_label": "변경 없음",
        "status_group": "complete",
        "auto_retryable": False,
    }
    blocked = _runner_display_status("cancelled", "dedup_blocked", "dedup_blocked: existing")
    assert blocked["display_status"] == "dedup_blocked"
    assert blocked["status_group"] == "blocked"
    action_required = _runner_display_status("error", "error", "auth_unavailable: relogin required")
    assert action_required["display_status"] == "auth_unavailable"
    assert action_required["status_group"] == "action_required"


@pytest.mark.asyncio
async def test_active_duplicate_lookup_is_scoped_by_project_hash_and_parallel_group():
    from app.api.pipeline_runner import _find_active_duplicate

    conn = _FakeConn(fetchrow_result={"job_id": "runner-a", "status": "running", "phase": "claude_code_work"})

    row = await _find_active_duplicate(conn, "AADS", "hash123", "group-a")

    assert row["job_id"] == "runner-a"
    query, args = conn.fetchrow_calls[0]
    assert "project = $1" in query
    assert "COALESCE(parallel_group, '') = $3" in query
    assert args[0] == "AADS"
    assert args[1] == "hash123"
    assert args[2] == "group-a"


@pytest.mark.asyncio
async def test_record_dedup_blocked_persists_terminal_blocked_job():
    from app.api.pipeline_runner import _record_dedup_blocked

    conn = _FakeConn()
    req = SimpleNamespace(
        project="AADS",
        instruction="same task",
        session_id="11111111-1111-1111-1111-111111111111",
        max_cycles=3,
        size="M",
        parallel_group="",
        depends_on="",
    )

    detail = await _record_dedup_blocked(
        conn,
        job_id="runner-blocked",
        req=req,
        instruction_hash="hash123",
        existing={"job_id": "runner-live", "status": "running", "phase": "claude_code_work"},
    )

    query, args = conn.execute_calls[0]
    assert "phase, max_cycles" in query
    assert "'cancelled', 'dedup_blocked'" in query
    assert args[0] == "runner-blocked"
    assert args[9].startswith("dedup_blocked: existing job runner-live")
    assert "auto_retryable=false" in args[10]
    assert detail.startswith("dedup_blocked:")


@pytest.mark.asyncio
async def test_runner_health_probe_reports_empty_logs_without_systemd_check():
    from app.api.pipeline_runner import _runner_health_probe

    conn = _FakeConn(fetchrow_result={"has_logs": False})
    row = {
        "job_id": "runner-live",
        "status": "running",
        "runner_pid": os.getpid(),
    }

    probe = await _runner_health_probe(conn, row)

    assert probe == {
        "task_logs": "empty",
        "runner_pid": os.getpid(),
        "proc_alive": True,
        "systemd": "not_checked_by_api",
    }

