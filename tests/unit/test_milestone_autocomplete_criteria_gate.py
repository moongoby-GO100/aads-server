"""마일스톤 자동 완료의 완료기준 게이트 (AADS-MILESTONE-AUTOCOMPLETE-CRITERIA-GATE-20261022)."""
import asyncio
import json

import pytest

from app.services import goal_manager

GOAL = "11111111-1111-4111-8111-111111111111"
MS = "33333333-3333-4333-8333-333333333333"


class Store:
    def __init__(self, status="in_progress", auto_advance=True, criteria=None, version=3):
        self.ms = {"status": status, "version": version, "auto_advance": auto_advance,
                   "completion_criteria": criteria, "evidence": None, "reported_at": None}
        self.links = [
            {"task_type": "pipeline_job", "task_id": "j1", "status": "completed", "goal_id": GOAL},
            {"task_type": "pipeline_job", "task_id": "j2", "status": "done", "goal_id": GOAL},
        ]
        self.updates = []
        self.advanced = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_):
                return False

        return _Tx()

    async def fetchrow(self, query, *args):
        if "FOR UPDATE" in query:
            return {"status": self.ms["status"], "version": self.ms["version"],
                    "auto_advance": self.ms["auto_advance"],
                    "completion_criteria": self.ms["completion_criteria"],
                    "completion_checklist": None}
        if "SELECT goal_id FROM milestones" in query:
            return {"goal_id": GOAL}
        raise AssertionError(query)

    async def fetch(self, query, *args):
        return self.links

    async def execute(self, query, *args):
        self.updates.append(query)
        if "SET status = 'completed'" in query:
            self.ms["status"] = "completed"
        elif "SET status = 'review'" in query:
            assert self.ms["status"] in ("pending", "in_progress", "blocked")
            self.ms["status"] = "review"
            self.ms["evidence"] = json.loads(args[2])
            self.ms["reported_at"] = "now"
        self.ms["version"] += 1
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
def check(monkeypatch):
    def run(store):
        machine = goal_manager.GoalStateMachine()

        async def pool():
            return _Pool(store)

        async def columns(_conn):
            return set()

        async def noop(*_a, **_k):
            return None

        async def advance(goal_id, milestone_id):
            store.advanced.append(milestone_id)

        monkeypatch.setattr(machine, "_pool", pool)
        monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
        monkeypatch.setattr(machine, "_mark_superseded_failures", noop)
        monkeypatch.setattr(goal_manager, "mark_explicit_retry_supersession", noop)
        monkeypatch.setattr(goal_manager, "archive_exhaustion_note", noop)
        monkeypatch.setattr(machine, "_advance_after_milestone", advance)
        return asyncio.run(machine.check_milestone_completion(MS))

    return run


def test_no_criteria_auto_advance_true_completes_and_advances(check):
    store = Store(criteria=None)
    result = check(store)
    assert result["completed"] is True
    assert store.ms["status"] == "completed"
    assert store.advanced == [MS]


def test_criteria_present_goes_to_review_without_advance(check):
    store = Store(criteria="08:00 실수신 캡처, sent 기록")
    result = check(store)
    assert result == {"milestone_id": MS, "completed": False, "status": "review",
                      "reason": "completion_criteria_gate"}
    assert store.ms["status"] == "review"
    assert store.ms["reported_at"] == "now"
    assert store.ms["evidence"]["linked_tasks_all_done"] is True
    assert store.ms["evidence"]["task_ids"] == ["j1", "j2"]
    assert store.advanced == []


def test_auto_advance_false_goes_to_review_with_reason(check):
    store = Store(auto_advance=False, criteria=None)
    result = check(store)
    assert result["completed"] is False
    assert result["status"] == "review"
    assert result["reason"] == "auto_advance_disabled"
    assert store.ms["status"] == "review"
    assert store.advanced == []


def test_already_review_is_idempotent(check):
    store = Store(status="review", criteria="완료기준 있음")
    result = check(store)
    assert result["completed"] is False
    assert result["status"] == "review"
    assert store.updates == []
    assert store.advanced == []
    assert check(store)["completed"] is False
    assert store.updates == []


@pytest.mark.parametrize("criteria", ["", "   ", "\n\t "])
def test_whitespace_only_criteria_is_treated_as_empty(check, criteria):
    store = Store(criteria=criteria)
    result = check(store)
    assert result["completed"] is True
    assert store.ms["status"] == "completed"
    assert store.advanced == [MS]


def test_already_completed_with_criteria_is_untouched(check):
    store = Store(status="completed", criteria="기준")
    result = check(store)
    assert result["completed"] is True and result.get("already_completed") is True
    assert store.updates == []
