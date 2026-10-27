"""목표 처리 순서(goals.priority_order) — 발송 정렬·순서 변경 API·초기값 멱등."""
from __future__ import annotations

import inspect
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from app.services import goal_dispatch, goal_manager, goal_priority_order
from app.services.goal_signals import _PRIORITY_RANK, priority_rank, priority_rank_case_sql

ROOT = Path(__file__).parents[2]
TENANT = str(uuid.uuid4())


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _dispatch_source() -> str:
    source = inspect.getsource(goal_dispatch.dispatch_pending_milestones)
    return _norm(re.sub(r"--[^\n]*", "", source))


def _eval_case(sql: str, priority: Any) -> int:
    """SQL CASE 식을 파싱해 같은 입력에 대한 순번을 계산한다."""
    key = str(priority or "").strip().upper()
    pairs = re.findall(r"WHEN '([^']*)' THEN (\d+)", sql)
    else_rank = int(re.search(r"ELSE (\d+) END", sql).group(1))
    return next((int(rank) for when, rank in pairs if when == key), else_rank)


# --- SQL 의 CASE 가 priority_rank() 와 일치 -------------------------------

@pytest.mark.parametrize(
    "value", [*_PRIORITY_RANK, "p1", " P2 ", None, "", "P9", "urgent"],
)
def test_case_sql_matches_priority_rank(value) -> None:
    assert _eval_case(priority_rank_case_sql("g.priority"), value) == priority_rank(value)


def test_dispatch_sql_embeds_shared_case_expression() -> None:
    expected = _norm(priority_rank_case_sql("g.priority")) + " AS goal_priority_rank"
    assert expected in _dispatch_source()


def test_list_goals_sql_embeds_shared_case_expression() -> None:
    source = _norm(inspect.getsource(goal_manager.GoalStateMachine.list_goals))
    assert source.count(_norm(priority_rank_case_sql("priority"))) == 2
    assert source.count("priority_order ASC NULLS LAST") == 2


# --- 발송 후보 정렬 ------------------------------------------------------

def test_dispatch_outer_order_by_clause() -> None:
    match = re.search(r"\) c .*?ORDER BY (.+?) LIMIT \$3", _dispatch_source())
    assert match
    assert match.group(1).lower() == (
        "goal_rn, goal_priority_rank, priority_order asc nulls last, "
        "coalesce(dispatched_at, load_deferred_since) asc nulls first, sequence_order"
    )
    inner = _dispatch_source().split("FROM milestones m")[0]
    assert "g.priority_order" in inner


def _order(candidates: list[dict]) -> list[str]:
    """바깥 ORDER BY 를 그대로 재현한다(goal_rn 은 목표별 1 로 가정)."""
    return [
        c["id"] for c in sorted(
            candidates,
            key=lambda c: (
                c.get("goal_rn", 1),
                priority_rank(c["priority"]),
                c["priority_order"] is None,
                c["priority_order"] or 0,
                c.get("dispatched_at") is not None,
                c.get("dispatched_at") or "",
                c.get("sequence_order", 1),
            ),
        )
    ]


def test_p0_goes_before_p2_even_with_unsent_p2() -> None:
    rows = [
        {"id": "p2", "priority": "P2", "priority_order": 1, "dispatched_at": None},
        {"id": "p0", "priority": "P0", "priority_order": 99, "dispatched_at": "2026-10-01"},
    ]
    assert _order(rows) == ["p0", "p2"]


def test_same_grade_follows_priority_order_and_null_is_last() -> None:
    rows = [
        {"id": "null", "priority": "P1", "priority_order": None, "dispatched_at": None},
        {"id": "three", "priority": "P1", "priority_order": 3, "dispatched_at": None},
        {"id": "one", "priority": "P1", "priority_order": 1, "dispatched_at": "2026-10-01"},
    ]
    assert _order(rows) == ["one", "three", "null"]


def test_goal_rn_still_comes_first() -> None:
    rows = [
        {"id": "p0-second-ms", "priority": "P0", "priority_order": 1, "goal_rn": 2, "dispatched_at": None},
        {"id": "p3-first-ms", "priority": "P3", "priority_order": 9, "goal_rn": 1, "dispatched_at": None},
    ]
    assert _order(rows) == ["p3-first-ms", "p0-second-ms"]


# --- 초기값 SQL ---------------------------------------------------------

def test_backfill_sql_is_idempotent_and_scoped() -> None:
    sql = _norm(goal_priority_order.BACKFILL_PRIORITY_ORDER_SQL)
    assert "WHERE g.priority_order IS NULL AND g.status IN ('draft', 'active', 'blocked')" in sql
    assert sql.endswith("WHERE g.id = ranked.id AND g.priority_order IS NULL")
    assert "PARTITION BY g.tenant_id" in sql
    assert f"ORDER BY {_norm(priority_rank_case_sql('g.priority'))}, g.created_at" in sql
    assert "COALESCE(MAX(priority_order), 0)" in sql
    for terminal in ("completed", "cancelled", "archived"):
        assert terminal not in sql


@pytest.mark.asyncio
async def test_backfill_executes_single_statement() -> None:
    conn = _FakeConn()
    await goal_priority_order.backfill_priority_order(conn)
    assert conn.executed == [goal_priority_order.BACKFILL_PRIORITY_ORDER_SQL]


def test_migration_files() -> None:
    up = (ROOT / "migrations/20261028_goals_priority_order.sql").read_text()
    down = (ROOT / "migrations/rollback/20261028_goals_priority_order.down.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS priority_order integer" in up
    assert "DROP COLUMN IF EXISTS priority_order" in down
    main_src = (ROOT / "app/main.py").read_text()
    assert '("goals", "priority_order", "INTEGER DEFAULT NULL")' in main_src
    assert "backfill_priority_order" in main_src


# --- API ---------------------------------------------------------------

class _Tx:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn

    async def __aenter__(self):
        self.conn.tx_entered += 1
        return self

    async def __aexit__(self, exc_type, *_):
        self.conn.tx_failed += 1 if exc_type else 0
        return False


class _FakeConn:
    def __init__(self, goals: dict[str, str] | None = None) -> None:
        self.goals = goals or {}  # goal_id -> tenant_id
        self.orders: dict[str, int] = {}
        self.executed: list[str] = []
        self.tx_entered = 0
        self.tx_failed = 0

    def transaction(self):
        return _Tx(self)

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if "unnest" in sql:
            ids, tenant = args
            for i, gid in enumerate(ids, 1):
                if self.goals.get(gid) == tenant:
                    self.orders[gid] = i
        return "UPDATE"

    async def fetch(self, sql, ids, tenant):
        return [{"id": g} for g in ids if self.goals.get(g) == tenant]

    async def fetchrow(self, sql, goal_id, tenant, order):
        if self.goals.get(goal_id) != tenant:
            return None
        self.orders[goal_id] = order
        return {"id": goal_id}


class _Pool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *_):
                return False

        return _Ctx()


@pytest.fixture
def api(monkeypatch):
    from app.routers import goals

    conn = _FakeConn()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _Pool(conn))
    ctx = {"tenant": {"id": TENANT}, "user": {"user_id": "u1"}}
    return goals, conn, ctx


@pytest.mark.asyncio
async def test_patch_sets_order_in_own_tenant(api) -> None:
    goals, conn, ctx = api
    gid = uuid.uuid4()
    conn.goals[str(gid)] = TENANT
    out = await goals.set_goal_priority_order(gid, goals.GoalOrderRequest(priority_order=3), ctx)
    assert out == {"goal_id": str(gid), "priority_order": 3}
    assert conn.orders[str(gid)] == 3


@pytest.mark.asyncio
async def test_patch_rejects_other_tenant_goal(api) -> None:
    goals, conn, ctx = api
    gid = uuid.uuid4()
    conn.goals[str(gid)] = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await goals.set_goal_priority_order(gid, goals.GoalOrderRequest(priority_order=1), ctx)
    assert exc.value.status_code == 404
    assert conn.orders == {}


def test_patch_request_rejects_non_positive() -> None:
    from pydantic import ValidationError

    from app.routers.goals import GoalOrderRequest

    with pytest.raises(ValidationError):
        GoalOrderRequest(priority_order=0)


@pytest.mark.asyncio
async def test_reorder_assigns_one_to_n_in_single_transaction(api) -> None:
    goals, conn, ctx = api
    ids = [uuid.uuid4() for _ in range(3)]
    for g in ids:
        conn.goals[str(g)] = TENANT
    shuffled = [ids[2], ids[0], ids[1]]
    out = await goals.reorder_goals_api(goals.GoalReorderRequest(goal_ids=shuffled), ctx)
    assert out["reordered"] == 3
    assert [conn.orders[str(g)] for g in shuffled] == [1, 2, 3]
    assert conn.tx_entered == 1


@pytest.mark.asyncio
async def test_reorder_rejects_foreign_or_unknown_goal_without_writing(api) -> None:
    goals, conn, ctx = api
    mine, foreign = uuid.uuid4(), uuid.uuid4()
    conn.goals[str(mine)] = TENANT
    conn.goals[str(foreign)] = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await goals.reorder_goals_api(goals.GoalReorderRequest(goal_ids=[mine, foreign]), ctx)
    assert exc.value.status_code == 404
    assert conn.orders == {}
    with pytest.raises(HTTPException) as exc:
        await goals.reorder_goals_api(goals.GoalReorderRequest(goal_ids=[mine, uuid.uuid4()]), ctx)
    assert exc.value.status_code == 404
    assert conn.orders == {}


@pytest.mark.asyncio
async def test_reorder_duplicate_ids_is_422_before_any_query(api) -> None:
    goals, conn, ctx = api
    gid = uuid.uuid4()
    conn.goals[str(gid)] = TENANT
    with pytest.raises(HTTPException) as exc:
        await goals.reorder_goals_api(goals.GoalReorderRequest(goal_ids=[gid, gid]), ctx)
    assert exc.value.status_code == 422
    assert conn.tx_entered == 0 and conn.orders == {}


def test_reorder_request_requires_valid_uuids_and_nonempty() -> None:
    from pydantic import ValidationError

    from app.routers.goals import GoalReorderRequest

    with pytest.raises(ValidationError):
        GoalReorderRequest(goal_ids=[])
    with pytest.raises(ValidationError):
        GoalReorderRequest(goal_ids=["not-a-uuid"])


def test_reorder_sql_runs_under_row_locks_and_tenant_filter() -> None:
    src = _norm(inspect.getsource(goal_priority_order.reorder_goals))
    assert "FOR UPDATE" in src
    assert "tenant_id = $2::uuid" in src
    assert "WITH ORDINALITY" in src


def test_routes_registered_with_member_dependency() -> None:
    from app.routers.goals import require_tenant_member, router

    found = {
        (m, r.path): r for r in router.routes for m in getattr(r, "methods", ())
    }
    for key in (("PATCH", "/goals/{goal_id}/order"), ("POST", "/goals/reorder")):
        assert key in found, key
        deps = [d.call for d in found[key].dependant.dependencies]
        assert require_tenant_member in deps


def test_responses_expose_priority_order() -> None:
    from app.routers import goals

    src = inspect.getsource(goals)
    assert "g.priority, g.priority_order" in src  # for-session
    assert "priority_order, " in inspect.getsource(goals.goal_board)
    assert '"priority_order": r.get("priority_order")' in inspect.getsource(
        goal_manager.GoalStateMachine.list_goals
    )
