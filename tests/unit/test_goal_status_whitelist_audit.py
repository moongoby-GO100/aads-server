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


# ─── update_goal 밖에서 goals.status 를 직접 쓰는 8개 경로 ───────────────────────────
# 2026-09-30 e1689c52 목표가 draft→active 로 바뀌었는데 감사행이 없었다. 감사가 update_goal
# 한 곳에만 붙어 있었기 때문이다. 아래는 자동 경로 8곳 각각이 (a) 실제 전이 시 1행,
# (b) 조건에 걸려 0행 갱신(또는 old == new)이면 0행을 남기는지 확인한다.

NEXT_GOAL_ID = "c0a11000-0917-4000-8000-000000000002"
MILESTONE_ID = "c0a11000-0917-4000-8000-0000000000a1"


class _PathDB:
    """goals 상태 + goal_status_audit. 그 밖의 SELECT 는 부분 문자열로 응답을 고른다.

    UPDATE goals 는 SQL 의 상태 조건(IN/=/!=)과 NOT EXISTS(`blocked_elsewhere`)를 흉내내
    실제 갱신 행수를 "UPDATE n" 으로 돌려준다. transaction() 은 재진입(savepoint)하며
    예외 시 그 수준의 스냅숏으로 되돌린다.
    """

    def __init__(self, goals=None, *, audit_table=True):
        self.goals = {gid: {"status": st, "tenant_id": TENANT_ID} for gid, st in (goals or {}).items()}
        self.audit: list[dict] = []
        self.audit_table = audit_table
        self.blocked_elsewhere = False
        self.rows: dict[str, object] = {}
        self.lists: dict[str, list] = {}
        self.vals: dict[str, object] = {}
        self.executed: list[str] = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        return _PathTx(self)

    def is_in_transaction(self):
        return False

    @staticmethod
    def _pick(table, query):
        for key, value in table.items():
            if key in query:
                return True, value
        return False, None

    async def fetchrow(self, query, *args):
        if "SELECT status, tenant_id::text AS tenant_id FROM goals" in query:
            goal = self.goals.get(str(args[0]))
            return dict(goal) if goal else None
        found, value = self._pick(self.rows, query)
        if found:
            return value
        raise AssertionError(query)

    async def fetch(self, query, *args):
        found, value = self._pick(self.lists, query)
        return value if found else []

    async def fetchval(self, query, *args):
        found, value = self._pick(self.vals, query)
        return value if found else None

    def _update_goals(self, query, args):
        new = re.search(r"SET status = '(\w+)'", query).group(1)
        goal = self.goals.get(str(args[0]))
        if goal is None:
            return "UPDATE 0"
        old = goal["status"]
        if "status IN ('draft', 'active')" in query and old not in ("draft", "active"):
            return "UPDATE 0"
        where = query.split("WHERE", 1)[1]
        if "g.status = 'blocked'" in where and old != "blocked":
            return "UPDATE 0"
        if "status != 'completed'" in query and old == "completed":
            return "UPDATE 0"
        if "NOT EXISTS" in query and self.blocked_elsewhere:
            return "UPDATE 0"
        goal["status"] = new
        return "UPDATE 1"

    async def execute(self, query, *args):
        self.executed.append(query)
        if query.startswith("INSERT INTO goal_status_audit"):
            if not self.audit_table:
                raise RuntimeError('relation "goal_status_audit" does not exist')
            goal_id, old, new, actor, source, tenant_id, note = args
            self.audit.append({"goal_id": goal_id, "old_status": old, "new_status": new,
                               "actor": actor, "source": source, "tenant_id": tenant_id})
            return "INSERT 0 1"
        if re.match(r"\s*UPDATE goals\b", query):
            return self._update_goals(query, args)
        return "UPDATE 1"


class _PathTx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.snapshot = (copy.deepcopy(self.db.goals), list(self.db.audit))
        return self

    async def __aexit__(self, exc_type, *_exc):
        if exc_type is not None:
            self.db.goals, self.db.audit = self.snapshot
        return False


def _auto_machine(db, monkeypatch, *, keep_progress=False):
    machine = _machine(db)

    async def no_op(*_a, **_k):
        return None

    monkeypatch.setattr(machine, "_trace", no_op)
    if not keep_progress:
        monkeypatch.setattr(machine, "_update_goal_progress", no_op)
    return machine


def _only_audit(db, source):
    assert [row["source"] for row in db.audit] == [source], db.audit
    return db.audit[0]


# 1 activate_goal ───────────────────────────────────────────────────────────────
def _activate_rows(db, status):
    db.rows["SELECT project, title, status FROM goals"] = {"project": "AADS", "title": "t", "status": status}
    db.rows["status = 'pending'"] = None


def test_activate_goal_records_transition(monkeypatch):
    db = _PathDB({GOAL_ID: "draft"})
    _activate_rows(db, "draft")
    asyncio.run(_auto_machine(db, monkeypatch).activate_goal(GOAL_ID))

    row = _only_audit(db, "activate_goal")
    assert (row["old_status"], row["new_status"]) == ("draft", "active")
    assert row["actor"] == "system:goal_manager"
    assert row["tenant_id"] == TENANT_ID


def test_activate_goal_without_row_change_records_nothing(monkeypatch):
    # 읽은 뒤 행이 사라져 UPDATE 0 — 감사행을 만들면 안 된다.
    db = _PathDB({})
    _activate_rows(db, "draft")
    asyncio.run(_auto_machine(db, monkeypatch).activate_goal(GOAL_ID))
    assert db.audit == []


# 2 task_failure_cascade ──────────────────────────────────────────────────────
def _cascade(db, monkeypatch):
    machine = _auto_machine(db, monkeypatch)

    async def no_op(*_a, **_k):
        return None

    async def exhausted(*_a, **_k):
        return {"created": False, "why": "retry_exhausted"}

    async def columns(_conn):
        return set()

    monkeypatch.setattr(machine, "_mark_superseded_failures", no_op)
    monkeypatch.setattr(goal_manager, "ensure_retry_candidate", exhausted)
    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    db.vals["FROM pipeline_jobs"] = "GO100"
    db.lists["SELECT DISTINCT milestone_id, goal_id"] = [{"milestone_id": MILESTONE_ID, "goal_id": GOAL_ID}]
    asyncio.run(machine.update_task_status("pipeline_job", "job-1", "failed"))


def test_task_failure_cascade_records_transition(monkeypatch):
    db = _PathDB({GOAL_ID: "active"})
    _cascade(db, monkeypatch)

    row = _only_audit(db, "task_failure_cascade")
    assert (row["old_status"], row["new_status"]) == ("active", "blocked")
    assert db.goals[GOAL_ID]["status"] == "blocked"


def test_task_failure_cascade_on_already_blocked_goal_records_nothing(monkeypatch):
    db = _PathDB({GOAL_ID: "blocked"})
    _cascade(db, monkeypatch)
    assert db.audit == []


# 3 milestone_unblock ─────────────────────────────────────────────────────────
def _unblock(db):
    db.rows["AS is_next_open"] = {"goal_id": GOAL_ID, "milestone_status": "blocked",
                                  "goal_status": db.goals[GOAL_ID]["status"], "is_next_open": True}
    return asyncio.run(GoalStateMachine()._recover_stale_milestone_block(
        db, MILESTONE_ID, has_failed_links=False,
    ))


def test_milestone_unblock_records_transition():
    db = _PathDB({GOAL_ID: "blocked"})
    assert _unblock(db) == "in_progress"

    row = _only_audit(db, "milestone_unblock")
    assert (row["old_status"], row["new_status"]) == ("blocked", "active")


def test_milestone_unblock_with_other_blocked_milestone_records_nothing():
    db = _PathDB({GOAL_ID: "blocked"})
    db.blocked_elsewhere = True
    _unblock(db)
    assert db.goals[GOAL_ID]["status"] == "blocked"
    assert db.audit == []


# 4 milestone_advance_promote ─────────────────────────────────────────────────
def _advance(db, monkeypatch, status):
    db.rows["SELECT g.id, g.project"] = {
        "id": GOAL_ID, "project": "AADS", "title": "t", "status": status,
        "parent_goal_id": None, "parent_status": None, "milestone_count": 1,
    }
    db.rows["status IN ('in_progress', 'review')"] = None
    db.lists["AND sequence_order = ("] = [{"id": MILESTONE_ID}]
    return asyncio.run(_auto_machine(db, monkeypatch).advance_goal(GOAL_ID))


def test_milestone_advance_promote_records_transition(monkeypatch):
    db = _PathDB({GOAL_ID: "draft"})
    _advance(db, monkeypatch, "draft")

    row = _only_audit(db, "milestone_advance_promote")
    assert (row["old_status"], row["new_status"]) == ("draft", "active")


def test_milestone_advance_promote_without_row_change_records_nothing(monkeypatch):
    db = _PathDB({})
    _advance(db, monkeypatch, "draft")
    assert db.audit == []


# 5 all_milestones_complete · 6 auto_activate_next ───────────────────────────
def _progress(db, monkeypatch, *, next_goal=True):
    db.rows["COUNT(*) AS total"] = {"total": 2, "completed": 2}
    db.rows["SELECT project FROM goals"] = {"project": "AADS"}
    db.rows["WHERE project = $1 AND status = 'draft'"] = {"id": NEXT_GOAL_ID} if next_goal else None
    asyncio.run(_auto_machine(db, monkeypatch, keep_progress=True)._update_goal_progress(GOAL_ID))


def test_all_milestones_complete_records_transition(monkeypatch):
    db = _PathDB({GOAL_ID: "active"})
    _progress(db, monkeypatch, next_goal=False)

    row = _only_audit(db, "all_milestones_complete")
    assert (row["old_status"], row["new_status"]) == ("active", "completed")


def test_all_milestones_complete_on_completed_goal_records_nothing(monkeypatch):
    db = _PathDB({GOAL_ID: "completed"})
    _progress(db, monkeypatch, next_goal=False)
    assert db.audit == []


def test_auto_activate_next_records_transition(monkeypatch):
    db = _PathDB({GOAL_ID: "completed", NEXT_GOAL_ID: "draft"})
    _progress(db, monkeypatch)

    row = _only_audit(db, "auto_activate_next")
    assert row["goal_id"] == NEXT_GOAL_ID
    assert (row["old_status"], row["new_status"]) == ("draft", "active")
    assert row["actor"] == "system:goal_manager"


def test_auto_activate_next_without_row_change_records_nothing(monkeypatch):
    db = _PathDB({GOAL_ID: "completed"})  # 다음 목표 행이 이미 사라짐 → UPDATE 0
    _progress(db, monkeypatch)
    assert db.audit == []


# 7 failure_retry_unblock ─────────────────────────────────────────────────────
@pytest.mark.parametrize("other_blocked,expected_audit", [(False, 1), (True, 0)])
def test_failure_retry_unblock_audits_only_real_transition(monkeypatch, other_blocked, expected_audit):
    from app.services import next_step_proposals
    from app.services.goal_failure_retry import ensure_retry_candidate
    from tests.unit.test_goal_failure_retry import FakeConn
    from tests.unit.test_goal_failure_retry import GOAL_ID as RETRY_GOAL_ID
    from tests.unit.test_goal_failure_retry import MILESTONE_ID as RETRY_MILESTONE_ID

    async def columns(_conn):
        return set()

    async def propose(**_kwargs):
        return {"cards": [{"id": "card-1"}]}

    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    conn = FakeConn()
    conn.goal_status = "blocked"
    conn.milestone_status = "blocked"
    conn.other_blocked = other_blocked

    asyncio.run(ensure_retry_candidate(
        conn, milestone_id=RETRY_MILESTONE_ID, goal_id=RETRY_GOAL_ID,
        failed_task_id="job-1", reason="linked_task_failed",
    ))

    assert len(conn.audit) == expected_audit
    if expected_audit:
        goal_id, old, new, actor, source, _tenant, _note = conn.audit[0]
        assert (goal_id, old, new, source) == (RETRY_GOAL_ID, "blocked", "active", "failure_retry_unblock")
        assert actor == "system:goal_failure_retry"


# 8 temporal_complete ─────────────────────────────────────────────────────────
def _temporal(db):
    from app.services.temporal_controller import TemporalController

    db.lists["FROM milestones"] = [{"id": MILESTONE_ID, "title": "m", "status": "in_progress", "due_date": None}]
    db.lists["FROM goal_task_links"] = [{"task_type": "runner", "task_id": "job-1"}]
    db.rows["FROM ohvis_tasks"] = {"status": "done"}
    asyncio.run(TemporalController()._check_goal(db, GOAL_ID, "AADS"))


def test_temporal_complete_records_transition():
    db = _PathDB({GOAL_ID: "active"})
    _temporal(db)

    row = _only_audit(db, "temporal_complete")
    assert (row["old_status"], row["new_status"]) == ("active", "completed")
    assert row["actor"] == "system:temporal_controller"


@pytest.mark.parametrize("goals", [{GOAL_ID: "completed"}, {}], ids=["same_status", "row_gone"])
def test_temporal_complete_without_transition_records_nothing(goals):
    db = _PathDB(goals)
    _temporal(db)
    assert db.audit == []


def test_audit_failure_on_automatic_path_keeps_transition(caplog):
    db = _PathDB({GOAL_ID: "active"}, audit_table=False)
    with caplog.at_level("WARNING", logger=goal_manager.logger.name):
        _temporal(db)
    assert db.goals[GOAL_ID]["status"] == "completed"
    assert db.audit == []
    assert any("goal_status_audit_failed" in r.getMessage() for r in caplog.records)


def test_no_unaudited_goal_status_write_remains():
    """goals.status 를 SET 하는 문장은 감사 헬퍼를 거친 것뿐이어야 한다(update_goal·진행률 되돌림 제외)."""
    import pathlib

    root = pathlib.Path(goal_manager.__file__).parent
    for name in ("goal_manager.py", "goal_failure_retry.py", "temporal_controller.py"):
        text = (root / name).read_text(encoding="utf-8")
        for match in re.finditer(r"UPDATE goals\b[^;]*?SET status = '", text):
            before = text[max(0, match.start() - 400):match.start()]
            assert "_audited_goal_status_update(" in before, f"{name}: {text[match.start():match.start() + 80]}"
