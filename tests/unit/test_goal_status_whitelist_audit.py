"""goal_manager.update_goal — status 화이트리스트 + goal_status_audit 기록.

2026-09-30 NTV2 목표가 status='paused'(paused_at NULL)로 직접 바뀌어 채팅 목표 패널과
착수 지시가 통째로 멈췄는데, 누가 바꿨는지 추적할 기록이 없었다. 여기서는
①허용 밖 값이 DB 에 닿지 않는지 ②status 가 실제로 바뀔 때만 감사 1행이 남는지를
상태를 가진 DB 더블로 확인한다.
"""
from __future__ import annotations

import asyncio
import copy
import re

import pytest
from fastapi import HTTPException

from app.services import goal_manager
from app.services.goal_manager import GoalStateMachine, _ALLOWED_GOAL_STATUS

GOAL_ID = "c0a11000-0917-4000-8000-000000000001"
TENANT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class _FakeGoalsDB:
    """goals 1행 + goal_status_audit. transaction() 은 예외 시 스냅숏으로 되돌린다."""

    def __init__(self, status="blocked", *, audit_table=True):
        self.goal = {"id": GOAL_ID, "tenant_id": TENANT_ID, "status": status, "title": "라일론"}
        self.audit: list[dict] = []
        self.audit_table = audit_table
        self.writes = 0

    # pool / conn / transaction 을 한 객체로 흉내낸다.
    def acquire(self):
        return self

    def transaction(self):
        return _Tx(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _matches(self, goal_id, tenant_id):
        return goal_id == self.goal["id"] and (tenant_id is None or tenant_id == self.goal["tenant_id"])

    async def fetchval(self, query, *args):
        assert query.lstrip().startswith("SELECT status FROM goals"), query
        goal_id, tenant_id = args
        return self.goal["status"] if self._matches(goal_id, tenant_id) else None

    async def fetchrow(self, query, *args):
        assert query.lstrip().startswith("UPDATE goals"), query
        columns = re.findall(r"(\w+) = \$(\d+)", query)
        goal_id, tenant_id = args[0], args[-1]
        if not self._matches(goal_id, tenant_id):
            return None
        self.writes += 1
        for column, index in columns:
            self.goal[column] = args[int(index) - 1]
        return {"id": self.goal["id"], "tenant_id": self.goal["tenant_id"]}

    async def execute(self, query, *args):
        assert query.lstrip().startswith("INSERT INTO goal_status_audit"), query
        if not self.audit_table:
            raise RuntimeError('relation "goal_status_audit" does not exist')
        goal_id, old, new, actor, source, tenant_id, note = args
        self.audit.append({"goal_id": goal_id, "old_status": old, "new_status": new,
                           "actor": actor, "source": source, "tenant_id": tenant_id, "note": note})
        return "INSERT 0 1"


class _Tx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.snapshot = (copy.deepcopy(self.db.goal), list(self.db.audit))
        return self

    async def __aexit__(self, exc_type, *_exc):
        if exc_type is not None:
            self.db.goal, self.db.audit = self.snapshot
        return False


def _machine(db):
    machine = GoalStateMachine()

    async def pool():
        return db

    machine._pool = pool
    return machine


def test_whitelist_is_exactly_the_six_statuses_readers_handle():
    assert _ALLOWED_GOAL_STATUS == {"draft", "active", "blocked", "completed", "cancelled", "archived"}
    assert "paused" not in _ALLOWED_GOAL_STATUS
    assert "in_progress" not in _ALLOWED_GOAL_STATUS


def test_allowed_status_updates_and_records_one_audit_row():
    db = _FakeGoalsDB(status="blocked")
    result = asyncio.run(_machine(db).update_goal(GOAL_ID, tenant_id=TENANT_ID, actor="user:ceo", status="active"))

    assert result == {"goal_id": GOAL_ID, "updated": ["status"]}
    assert db.goal["status"] == "active"
    assert len(db.audit) == 1
    row = db.audit[0]
    assert (row["old_status"], row["new_status"]) == ("blocked", "active")
    assert row["goal_id"] == GOAL_ID
    assert row["source"] == "update_goal"
    assert row["tenant_id"] == TENANT_ID
    assert row["actor"] == "user:ceo"


def test_paused_is_rejected_without_touching_db_and_points_to_pause_endpoint():
    db = _FakeGoalsDB(status="active")
    result = asyncio.run(_machine(db).update_goal(GOAL_ID, tenant_id=TENANT_ID, status="paused"))

    assert result["error"] == "invalid_status"
    assert f"POST /api/v1/goals/{GOAL_ID}/pause" in result["message"]
    assert "paused_at" in result["message"]
    assert db.goal["status"] == "active"
    assert db.writes == 0
    assert db.audit == []


def test_title_only_update_records_no_audit_row():
    db = _FakeGoalsDB(status="active")
    result = asyncio.run(_machine(db).update_goal(GOAL_ID, tenant_id=TENANT_ID, title="새 제목"))

    assert result == {"goal_id": GOAL_ID, "updated": ["title"]}
    assert db.goal["title"] == "새 제목"
    assert db.audit == []


def test_same_status_update_is_idempotent_for_audit():
    db = _FakeGoalsDB(status="active")
    result = asyncio.run(_machine(db).update_goal(GOAL_ID, tenant_id=TENANT_ID, status="active"))

    assert "error" not in result
    assert db.audit == []


def test_audit_failure_does_not_block_status_change(caplog):
    db = _FakeGoalsDB(status="blocked", audit_table=False)
    with caplog.at_level("WARNING", logger=goal_manager.logger.name):
        result = asyncio.run(_machine(db).update_goal(GOAL_ID, tenant_id=TENANT_ID, status="completed"))

    assert "error" not in result
    assert db.goal["status"] == "completed"
    assert any("goal_status_audit_failed" in r.getMessage() for r in caplog.records)


def test_chat_session_contextvar_takes_precedence_as_actor():
    from app.services.tool_executor import current_chat_session_id

    db = _FakeGoalsDB(status="blocked")

    async def run():
        token = current_chat_session_id.set("sess-123")
        try:
            return await _machine(db).update_goal(GOAL_ID, tenant_id=TENANT_ID, actor="user:ceo", status="active")
        finally:
            current_chat_session_id.reset(token)

    asyncio.run(run())
    assert db.audit[0]["actor"] == "chat_session:sess-123"


def test_unknown_actor_stays_null():
    db = _FakeGoalsDB(status="blocked")
    asyncio.run(_machine(db).update_goal(GOAL_ID, status="archived"))
    assert db.audit[0]["actor"] is None


def test_put_route_returns_400_with_actionable_message(monkeypatch):
    from app.routers import goals

    db = _FakeGoalsDB(status="active")
    machine = _machine(db)
    monkeypatch.setattr(goal_manager, "goal_state_machine", machine)

    async def validated(goal_id, context, *, member=False):
        return TENANT_ID

    monkeypatch.setattr(goals, "_validated_tenant_goal", validated)
    context = {"tenant": {"id": TENANT_ID}, "user": {"user_id": "ceo"}}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.update_goal(GOAL_ID, goals.GoalUpdateRequest(status="paused"), context))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "invalid_status"
    assert "/pause" in exc.value.detail["message"]
    assert db.goal["status"] == "active"

    result = asyncio.run(goals.update_goal(GOAL_ID, goals.GoalUpdateRequest(status="blocked"), context))
    assert result["updated"] == ["status"]
    assert db.audit[-1]["actor"] == "user:ceo"
