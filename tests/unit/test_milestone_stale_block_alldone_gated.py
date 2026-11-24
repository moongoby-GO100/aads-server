"""전부 완료 + 완료기준 게이트에서도 stale blocked 를 복구한다 (AADS-MILESTONE-STALE-BLOCK-ALLDONE-GATED-20261124)."""
import asyncio

import pytest

from app.services import goal_manager

GOAL = "11111111-1111-4111-8111-111111111111"
MS = "33333333-3333-4333-8333-333333333333"


class Conn:
    def __init__(self, ms_status="blocked", goal_status="active", is_next_open=True):
        self.ms_status = ms_status
        self.goal_status = goal_status
        self.is_next_open = is_next_open
        self.links = [
            {"task_type": "pipeline_job", "task_id": "runner-a", "status": "completed", "goal_id": GOAL},
            {"task_type": "pipeline_job", "task_id": "runner-b", "status": "completed", "goal_id": GOAL},
        ]

    async def fetch(self, query, *args):
        return self.links

    async def fetchrow(self, query, *args):
        if "earlier.sequence_order" in query:
            return {"goal_id": GOAL, "milestone_status": self.ms_status,
                    "goal_status": self.goal_status, "is_next_open": self.is_next_open}
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "UPDATE milestones" in query and "SET status = $2" in query:
            if self.ms_status == "blocked":
                self.ms_status = args[1]
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_):
                return False

        return _Acq()


@pytest.fixture
def run(monkeypatch):
    def _run(conn, outcome):
        machine = goal_manager.GoalStateMachine()

        async def pool():
            return _Pool(conn)

        async def columns(_c):
            return set()

        async def noop(*_a, **_k):
            return None

        async def gate(_conn, _mid, linked_task_ids=None):
            return outcome

        monkeypatch.setattr(machine, "_pool", pool)
        monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
        monkeypatch.setattr(machine, "_mark_superseded_failures", noop)
        monkeypatch.setattr(goal_manager, "mark_explicit_retry_supersession", noop)
        monkeypatch.setattr(goal_manager, "_audited_goal_status_update", noop)
        monkeypatch.setattr(machine, "_complete_milestone_if_checklist_met", gate)
        return asyncio.run(machine.check_milestone_completion(MS))

    return _run


def test_gated_blocked_recovers_to_in_progress(run):
    conn = Conn()
    result = run(conn, {"result": "gated", "status": "blocked", "reason": "completion_criteria_gate"})
    assert result["completed"] is False
    assert result["status"] == "in_progress"
    assert result["block_recovered"] is True
    assert result["reason"] == "completion_criteria_gate"
    assert conn.ms_status == "in_progress"


def test_gated_after_reject_recovers_to_pending_when_not_next(run):
    conn = Conn(is_next_open=False)
    result = run(conn, {"result": "gated_after_reject", "status": "blocked"})
    assert result["status"] == "pending"
    assert result["block_recovered"] is True
    assert result["completed"] is False


def test_unmet_blocked_recovers_to_in_progress(run):
    conn = Conn()
    gate = {"status": "blocked", "unmet": [{"step": "4.1"}]}
    result = run(conn, {"result": "unmet", "gate": gate})
    assert result["completed"] is False
    assert result["reason"] == "checklist_unmet"
    assert result["status"] == "in_progress"
    assert result["block_recovered"] is True
    assert conn.ms_status == "in_progress"


def test_gated_review_status_not_touched(run):
    conn = Conn(ms_status="review")
    result = run(conn, {"result": "gated", "status": "review", "reason": "completion_criteria_gate"})
    assert result["status"] == "review"
    assert "block_recovered" not in result
    assert conn.ms_status == "review"


def test_paused_goal_not_recovered(run):
    conn = Conn(goal_status="paused")
    result = run(conn, {"result": "gated", "status": "blocked", "reason": "x"})
    assert result["status"] == "blocked"
    assert "block_recovered" not in result


def test_version_conflict_unchanged(run):
    conn = Conn()
    result = run(conn, {"result": "version_conflict"})
    assert result == {"milestone_id": MS, "completed": False, "reason": "version_conflict"}
    assert conn.ms_status == "blocked"
