"""목표 진행률 단일 공식 + 반려·재작업·배포 오탐 job 이 목표를 blocked 로 만들지 않는 cascade 보정."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services import goal_manager, goal_progress
from tests.unit.test_goal_failure_retry import GOAL_ID, MILESTONE_ID, FakeConn, FakePool

REWORK_INSTRUCTION = (
    f"GOAL_ID: {GOAL_ID}\nMILESTONE_ID: {MILESTONE_ID}\n"
    "REWORK_OF: runner-aaaaaaaa\nTASK_ID: X\n"
)


class ProgressConn:
    """goal_manager._update_goal_progress 가 읽고 쓰는 최소 면."""

    def __init__(self, *, finished=4, counted=44, ms_total=3, ms_done=0, task_table=True):
        self.finished, self.counted = finished, counted
        self.ms_total, self.ms_done = ms_total, ms_done
        self.task_table = task_table
        self.writes = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_):
                return False
        return _Tx()

    async def fetchrow(self, sql, *args):
        if "FROM work_items" in sql:
            if not self.task_table:
                raise RuntimeError('relation "work_items" does not exist')
            return {"finished": self.finished, "counted": self.counted}
        if "FROM milestones" in sql:
            return {"total": self.ms_total, "completed": self.ms_done}
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.writes.append((sql, args))
        return "UPDATE 1"


def _run_progress(monkeypatch, conn):
    machine = goal_manager.GoalStateMachine()

    async def pool():
        return FakePool(conn)

    async def no_op(*_a, **_k):
        return None

    monkeypatch.setattr(machine, "_pool", pool)
    monkeypatch.setattr(goal_manager, "sync_goal_grants", no_op)
    asyncio.run(machine._update_goal_progress(GOAL_ID))
    return next(args for sql, args in conn.writes if "UPDATE goals" in sql and "progress = $2" in sql)


def test_task_progress_survives_goal_manager_path(monkeypatch):
    conn = ProgressConn(finished=4, counted=44, ms_total=3, ms_done=0)
    assert _run_progress(monkeypatch, conn) == (GOAL_ID, 0.09)


def test_progress_falls_back_to_milestones_without_tasks(monkeypatch):
    conn = ProgressConn(finished=0, counted=0, ms_total=4, ms_done=1)
    assert _run_progress(monkeypatch, conn) == (GOAL_ID, 0.25)


def test_progress_falls_back_when_work_items_unavailable(monkeypatch):
    conn = ProgressConn(task_table=False, ms_total=2, ms_done=1)
    assert _run_progress(monkeypatch, conn) == (GOAL_ID, 0.5)


def test_task_formula_excludes_cancelled_and_optional():
    sql = goal_progress._TASK_STATS_SQL
    assert "type = 'task'" in sql
    assert "status <> 'cancelled'" in sql
    assert "acceptance_criteria->>'optional'" in sql


def test_both_writers_use_the_shared_formula():
    root = goal_manager.__file__.rsplit("/", 1)[0]
    for name in ("goal_manager.py", "goal_workflow_approval.py"):
        with open(f"{root}/{name}", encoding="utf-8") as fh:
            assert "goal_progress_ratio(" in fh.read(), name


class CascadeConn(FakeConn):
    def __init__(self):
        super().__init__(owner=None)  # 재작업 후보를 못 만들어 cascade 가 목표 차단까지 가는 상태
        self.followups = []
        self.job_row = None

    async def fetch(self, query, *args):
        if "FROM goal_task_links gl" in query:
            return self.followups
        return await super().fetch(query, *args)

    async def fetchrow(self, query, *args):
        if "SELECT error_detail, logs FROM pipeline_jobs" in query:
            return self.job_row
        return await super().fetchrow(query, *args)


@pytest.fixture
def cascade(monkeypatch):
    conn = CascadeConn()
    machine = goal_manager.GoalStateMachine()

    async def pool():
        return FakePool(conn)

    async def columns(_conn):
        return set()

    async def no_op(*_a, **_k):
        return None

    monkeypatch.setattr(machine, "_pool", pool)
    monkeypatch.setattr(machine, "_mark_superseded_failures", no_op)
    monkeypatch.setattr(machine, "_trace", no_op)
    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    return conn, machine


def _fail(machine, task_id="runner-aaaaaaaa", status="failed", phase=None):
    return asyncio.run(machine.update_task_status_with_phase("pipeline_job", task_id, status, phase))


def test_rejected_job_with_rework_successor_does_not_block_goal(cascade):
    conn, machine = cascade
    conn.followups = [{"job_id": "runner-bbbbbbbb", "instruction": REWORK_INSTRUCTION}]

    _fail(machine, status="rejected")

    assert conn.goal_status == "active"
    assert conn.audit == []


def test_retry_of_link_successor_also_exempts(cascade):
    conn, machine = cascade
    conn.followups = [{
        "job_id": "runner-bbbbbbbb",
        "instruction": "GOAL_ID: x\nRETRY_OF_LINK: runner-aaaaaaaa\n",
    }]

    _fail(machine, status="error")

    assert conn.goal_status == "active"


def test_mention_in_body_is_not_a_rework(cascade):
    conn, machine = cascade
    conn.followups = [{
        "job_id": "runner-bbbbbbbb",
        "instruction": "본문에서 runner-aaaaaaaa 를 언급만 한다\n",
    }]

    _fail(machine)

    assert conn.goal_status == "blocked"


@pytest.mark.parametrize("status,phase", [
    ("cancelled", "blocked_dependency"), ("rejected_done", "rejected_done"),
    ("rejected", "blocked_dependency"),
])
def test_blocked_dependency_and_rejected_done_do_not_block_goal(cascade, status, phase):
    conn, machine = cascade

    _fail(machine, status=status, phase=phase)

    assert conn.goal_status == "active"
    assert conn.audit == []


def test_deploy_gate_false_positive_with_pushed_commit_does_not_block_goal(cascade):
    conn, machine = cascade
    conn.job_row = {
        "error_detail": "go100-frontend:deploy_gate_blocked",
        "logs": json.dumps([{"event": "push_succeeded", "exit_code": 0}]),
    }

    _fail(machine, status="error")

    assert conn.goal_status == "active"


def test_deploy_gate_error_without_push_still_blocks_goal(cascade):
    conn, machine = cascade
    conn.job_row = {"error_detail": "go100-frontend:deploy_gate_blocked", "logs": []}

    _fail(machine, status="error")

    assert conn.goal_status == "blocked"


def test_real_failure_blocks_goal_and_records_job_id_in_audit_note(cascade):
    conn, machine = cascade
    conn.job_row = {"error_detail": "tests_failed", "logs": []}

    _fail(machine, task_id="runner-aaaaaaaa", status="failed", phase="failed")

    assert conn.goal_status == "blocked"
    assert len(conn.audit) == 1
    goal_id, old, new, _actor, source, _tenant, note = conn.audit[0]
    assert (goal_id, old, new, source) == (GOAL_ID, "active", "blocked", "task_failure_cascade")
    assert "runner-aaaaaaaa" in note
