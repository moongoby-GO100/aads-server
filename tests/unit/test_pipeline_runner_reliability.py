from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


_TENANT_ID = "2d701a8c-9596-4757-8588-faa4f7837112"


class _FakeConn:
    def __init__(self, *, fetchrow_result=None, fetch_result=None) -> None:
        self.fetchrow_result = fetchrow_result
        self.fetch_result = fetch_result or []
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_result

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return self.fetch_result

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1" if "UPDATE pipeline_jobs" in query else "INSERT 0 1"


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


def test_extract_target_files_normalizes_server_and_dashboard_paths():
    from app.api.pipeline_runner import _extract_target_files

    files = _extract_target_files(
        "파일: app/api/pipeline_runner.py, "
        "/root/aads/aads-dashboard/src/app/chat/page.tsx "
        "package.json deploy.sh"
    )

    assert "server:app/api/pipeline_runner.py" in files
    assert "dashboard:src/app/chat/page.tsx" in files
    assert "dashboard:package.json" in files
    assert "server:deploy.sh" in files


@pytest.mark.asyncio
async def test_find_active_file_conflict_detects_overlapping_instruction_files():
    from app.api.pipeline_runner import _find_active_file_conflict

    conn = _FakeConn(
        fetch_result=[
            {
                "job_id": "runner-live",
                "instruction": "파일: app/api/pipeline_runner.py 수정",
                "status": "running",
                "phase": "claude_code_work",
            }
        ]
    )

    conflict = await _find_active_file_conflict(
        conn,
        project="AADS",
        target_files={"server:app/api/pipeline_runner.py"},
        tenant_id=_TENANT_ID,
    )

    assert conflict == {
        "job_id": "runner-live",
        "status": "running",
        "phase": "claude_code_work",
        "overlap": ["server:app/api/pipeline_runner.py"],
    }


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
        tenant_id=_TENANT_ID,
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
        "project": "AADS",
        "status": "running",
        "runner_pid": os.getpid(),
    }

    probe = await _runner_health_probe(conn, row)

    assert probe == {
        "task_logs": "empty",
        "runner_pid": os.getpid(),
        "proc_alive": True,
        "proc_scope": "local_proc",
        "suspect_stale": False,
        "reasons": ["empty_task_logs"],
        "systemd": "not_checked_by_api",
    }


@pytest.mark.asyncio
async def test_runner_health_probe_does_not_check_remote_pid_as_local_dead():
    from app.api.pipeline_runner import _runner_health_probe

    conn = _FakeConn(fetchrow_result={"has_logs": True})
    row = {
        "job_id": "runner-remote",
        "project": "NTV2",
        "status": "running",
        "runner_pid": 999999,
    }

    assert await _runner_health_probe(conn, row) is None


@pytest.mark.asyncio
async def test_cleanup_dead_local_runner_processes_skips_remote_project():
    from app.api.pipeline_runner import _cleanup_dead_local_runner_processes

    conn = _FakeConn(fetch_result=[{"job_id": "runner-remote", "runner_pid": 999999}])

    cleaned = await _cleanup_dead_local_runner_processes(conn, "NTV2")

    assert cleaned == 0
    assert conn.fetch_calls == []
    assert conn.execute_calls == []


@pytest.mark.asyncio
async def test_cleanup_dead_local_runner_processes_marks_dead_aads_pid():
    from app.api.pipeline_runner import _cleanup_dead_local_runner_processes

    conn = _FakeConn(fetch_result=[{"job_id": "runner-dead", "runner_pid": 999999}])

    cleaned = await _cleanup_dead_local_runner_processes(conn, "AADS", min_age_seconds=1)

    assert cleaned == 1
    query, args = conn.execute_calls[0]
    assert "error_detail = 'process_died'" in query
    assert args[0] == "runner-dead"
    assert "PID=999999" in args[1]
