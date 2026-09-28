"""The new milestone field is visible only when it has a value."""
import asyncio

from app.services.goal_manager import GoalStateMachine


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, checklist):
        self.checklist = checklist

    async def fetchrow(self, sql, *_args):
        assert "FROM goals" in sql
        return {
            "id": "goal-1", "project": "AADS", "title": "Goal", "priority": "P2",
            "status": "active", "description": "Description", "success_criteria": None,
            "progress": 0, "created_at": None, "completed_at": None,
        }

    async def fetch(self, sql, *_args):
        if "FROM milestones" in sql:
            assert "completion_checklist" in sql
            return [{
                "id": "milestone-1", "title": "Milestone", "sequence_order": 1,
                "status": "pending", "auto_advance": True,
                "completion_criteria": "Existing text", "completion_checklist": self.checklist,
                "started_at": None, "completed_at": None,
            }]
        assert "FROM goal_task_links" in sql
        return []


def _status(monkeypatch, checklist):
    machine = GoalStateMachine()

    async def pool():
        return _Pool(_Conn(checklist))

    monkeypatch.setattr(machine, "_pool", pool)
    monkeypatch.setattr("app.services.goal_manager.link_optional_columns", lambda _conn: _columns())
    return asyncio.run(machine.get_goal_status("goal-1"))


async def _columns():
    return set()


def test_goal_status_without_checklist_keeps_previous_response_contract(monkeypatch):
    status = _status(monkeypatch, None)
    assert status == {
        "goal_id": "goal-1", "project": "AADS", "title": "Goal", "priority": "P2",
        "status": "active", "description": "Description", "success_criteria": "Description",
        "progress": 0, "milestones_total": 1, "milestones_completed": 0,
        "milestones": [{
            "id": "milestone-1", "title": "Milestone", "sequence": 1,
            "status": "pending", "auto_advance": True,
            "completion_criteria": "Existing text", "started_at": None,
            "completed_at": None, "tasks": [],
        }],
    }


def test_goal_status_includes_structured_checklist(monkeypatch):
    status = _status(monkeypatch, '[{"criterion":"Checks pass"}]')
    assert status["milestones"][0]["completion_checklist"] == [
        {"criterion": "Checks pass"},
    ]
    assert status["milestones"][0]["completion_criteria"] == "Existing text"
