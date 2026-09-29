"""Goal pause blocks automatic sends while preserving CEO manual direction."""
from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import goal_intervene, milestone_review, orchestration_limits
from tests.unit.test_goal_dispatch_progress_gate import (
    run_cycle as dispatch_run_cycle,  # noqa: F401
)


def test_goal_pause_reads_flag_independently_of_status(monkeypatch):
    from app.core import db_pool

    class Pool:
        def __init__(self):
            self.row = {"paused_at": "now", "paused_reason": None}

        async def fetchrow(self, query, goal_id):
            assert "paused_at" in query and "status" not in query
            assert goal_id == "goal"
            return self.row

    pool = Pool()
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    assert asyncio.run(orchestration_limits.goal_paused("goal")) == (
        True, "대표님이 목표를 멈춤"
    )
    pool.row = {"paused_at": None, "paused_reason": "old reason"}
    assert asyncio.run(orchestration_limits.goal_paused("goal")) == (False, "")


def test_dispatch_pause_and_resume(monkeypatch, dispatch_run_cycle):  # noqa: F811
    paused = True

    async def gate(_goal_id):
        return (paused, "대표님이 목표를 멈춤" if paused else "")

    monkeypatch.setattr(orchestration_limits, "goal_paused", gate)
    answered = datetime.now(UTC) - timedelta(hours=1)
    result, conn, sent = dispatch_run_cycle(answered)
    assert result["sent"] == 0
    assert conn.claims == 0  # dispatch_count increment happens only in the claim
    assert sent == []

    paused = False
    result, conn, sent = dispatch_run_cycle(answered)
    assert result["sent"] == 1
    assert conn.claims == 1
    assert len(sent) == 1


def test_review_pause_and_resume(monkeypatch):
    from app.core import db_pool
    from app.services import chat_service

    class Conn:
        updates = 0

        async def fetch(self, *_args):
            return [{"goal_id": "goal", "milestone_id": "milestone", "title": "M",
                     "owner": "owner", "owner_session": "owner", "goal_title": "G",
                     "project": "AADS", "review_ask_count": 0, "review_asked_at": None,
                     "completion_criteria": "done", "evidence": {}}]

        async def fetchval(self, *_args):
            return "lead"

        async def execute(self, *_args):
            self.updates += 1

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            pool = self

            class Context:
                async def __aenter__(self):
                    return pool.conn

                async def __aexit__(self, *_args):
                    return False

            return Context()

    conn = Conn()
    monkeypatch.setattr(db_pool, "get_pool", lambda: Pool(conn))
    monkeypatch.setattr(
        milestone_review, "_lead_session", lambda *_: asyncio.sleep(0, result="lead")
    )
    sent = []

    async def stream(**kwargs):
        sent.append(kwargs)
        yield "ok"

    monkeypatch.setattr(chat_service, "send_message_stream", stream)
    paused = True

    async def gate(_goal_id):
        return paused, "paused" if paused else ""

    monkeypatch.setattr(orchestration_limits, "goal_paused", gate)
    assert asyncio.run(milestone_review.ask_pending_reviews()) == {"asked": 0, "escalated": 0}
    assert conn.updates == 0 and sent == []
    paused = False
    assert asyncio.run(milestone_review.ask_pending_reviews())["asked"] == 1
    assert conn.updates == 1 and len(sent) == 1


def test_ceo_direct_still_sends_when_goal_paused(monkeypatch):
    from app.core import db_pool
    from app.services import chat_service

    class Pool:
        async def fetchval(self, *_args):
            return 1

        def acquire(self):
            pool = self

            class Context:
                async def __aenter__(self):
                    return pool

                async def __aexit__(self, *_args):
                    return False

            return Context()

    async def owners(*_args):
        return [{"id": "session", "role_key": "Lead"}]

    sent = []

    async def stream(**kwargs):
        sent.append(kwargs)
        yield "ok"

    async def forbidden_gate(_goal_id):
        raise AssertionError("direct must not consult goal pause")

    monkeypatch.setattr(db_pool, "get_pool", Pool)
    monkeypatch.setattr(goal_intervene, "_owner_sessions", owners)
    monkeypatch.setattr(chat_service, "send_message_stream", stream)
    monkeypatch.setattr(orchestration_limits, "goal_paused", forbidden_gate)
    result = asyncio.run(goal_intervene.direct("goal", "대표님 지시", tenant_id="tenant"))
    assert result["sent"] == ["Lead"]
    assert len(sent) == 1


def test_pause_api_handlers_round_trip_and_tenant_scope(monkeypatch):
    from app.core import db_pool
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-goal-pause-secret")
    from app.routers import goals

    tenant_id = "00000000-0000-4000-8000-000000000002"

    class Store:
        paused_at = None
        paused_reason = None
        paused_by = None

        async def fetchrow(self, query, goal_id, tenant_id, *values):
            if (goal_id != "00000000-0000-4000-8000-000000000001"
                    or tenant_id != "00000000-0000-4000-8000-000000000002"):
                return None
            if "paused_at = COALESCE" in query:
                self.paused_at = self.paused_at or "now"
                self.paused_reason, self.paused_by = values
            else:
                self.paused_at = self.paused_reason = self.paused_by = None
            return {"id": goal_id, "paused_at": self.paused_at,
                    "paused_reason": self.paused_reason, "paused_by": self.paused_by}

        def acquire(self):
            store = self

            class Context:
                async def __aenter__(self):
                    return store

                async def __aexit__(self, *_args):
                    return False

            return Context()

    store = Store()
    monkeypatch.setattr(db_pool, "get_pool", lambda: store)
    context = {"tenant": {"id": tenant_id},
               "user": {"id": "ceo", "is_internal_admin": True}}
    goal_id = "00000000-0000-4000-8000-000000000001"
    async def round_trip():
        stopped = await goals.pause_goal(goal_id, goals.GoalPauseRequest(reason="재기획"), context)
        after_pause = (store.paused_at, store.paused_reason, store.paused_by)
        resumed = await goals.resume_goal(goal_id, context)
        return stopped, after_pause, resumed

    stopped, after_pause, resumed = asyncio.run(round_trip())
    assert stopped == {"goal_paused": True, "goal_paused_reason": "재기획"}
    assert after_pause == ("now", "재기획", "ceo")
    assert resumed == {"goal_paused": False, "goal_paused_reason": None}
    assert (store.paused_at, store.paused_reason, store.paused_by) == (None, None, None)


def test_tenant_member_cannot_pause_or_resume_ceo_goal():
    from app.routers import goals

    member = {"tenant": {"id": "tenant"},
              "user": {"id": "member", "is_internal_admin": False}}
    for call in (goals.pause_goal("goal", goals.GoalPauseRequest(reason="stop"), member),
                 goals.resume_goal("goal", member)):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call)
        assert exc.value.status_code == 403


def test_detached_send_rechecks_pause_and_refunds_claim(monkeypatch):
    from app.core import db_pool
    from app.services import goal_dispatch

    updates = []

    class Pool:
        async def execute(self, query, *args):
            updates.append((query, args))

    async def paused(_goal_id):
        return True, "stop"

    monkeypatch.setattr(db_pool, "get_pool", Pool)
    monkeypatch.setattr(orchestration_limits, "goal_paused", paused)
    asyncio.run(goal_dispatch._send_milestone(
        goal_id="goal", milestone_id="milestone", session_id="session",
        message="message", attempt=1, project="AADS", count_after=1,
    ))
    assert len(updates) == 1
    assert "dispatch_count = dispatch_count - 1" in updates[0][0]


def test_paused_missing_owner_does_not_create_approval(monkeypatch, dispatch_run_cycle):  # noqa: F811
    from app.services import goal_dispatch
    from tests.unit import test_goal_dispatch_progress_gate as progress

    original = progress._row

    def missing_owner():
        row = original()
        row["session_id"] = None
        return row

    async def paused(_goal_id):
        return True, "stop"

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("approval card created while paused")

    monkeypatch.setattr(progress, "_row", missing_owner)
    monkeypatch.setattr(goal_dispatch, "_handle_missing_owner", forbidden)
    monkeypatch.setattr(orchestration_limits, "goal_paused", paused)
    result, conn, sent = dispatch_run_cycle(None)
    assert result["owner_requests"] == 0 and conn.claims == 0 and sent == []


def test_relay_binds_only_unambiguous_shared_goal(monkeypatch):
    from app.core import db_pool
    from app.services import session_relay

    class Pool:
        def __init__(self):
            self.rows = [{"goal_id": "00000000-0000-4000-8000-000000000001"},
                         {"goal_id": "00000000-0000-4000-8000-000000000002"}]

        async def fetch(self, *_args):
            return self.rows

    pool = Pool()
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    assert asyncio.run(session_relay._relay_goal("origin", "target", "unrelated")) is None
    assert asyncio.run(session_relay._relay_goal(
        "origin", "target", "GOAL_ID: 00000000-0000-4000-8000-000000000002"
    )) == pool.rows[1]["goal_id"]
    pool.rows = pool.rows[:1]
    assert asyncio.run(session_relay._relay_goal("origin", "target", "work")) == pool.rows[0]["goal_id"]


def test_paused_relay_question_stays_queued(monkeypatch):
    from app.core import db_pool
    from app.services import session_relay

    writes = []

    class Pool:
        async def execute(self, query, *args):
            writes.append((query, args))

    async def paused(_goal_id):
        return True, "stop"

    monkeypatch.setattr(db_pool, "get_pool", Pool)
    monkeypatch.setattr(orchestration_limits, "goal_paused", paused)
    asyncio.run(session_relay._run_relay("relay", "target", "prompt", "origin", "question", "goal"))
    assert len(writes) == 1 and "status='queued'" in writes[0][0]


class _RelayPool:
    """dispatch_queued_relays 용 가짜 풀. 조회 SQL 과 쓰기를 기록한다."""

    def __init__(self, replies=None, queued=None):
        self.replies = replies or []
        self.queued = queued or []
        self.fetches = []
        self.writes = []
        self.events = []

    async def fetch(self, query, *_args):
        self.fetches.append(query)
        self.events.append("fetch")
        if "r.status = 'blocked'" in query:
            return self.replies
        if "WHERE status = 'queued'" in query:
            return self.queued
        return []

    async def fetchrow(self, *_args):
        return {"title": "origin", "role_key": "role"}

    async def fetchval(self, *_args):
        return "role"

    async def execute(self, query, *args):
        self.writes.append((query, args))
        self.events.append("expire" if "queue_expired" in query else "write")
        if "SET status='pending'" in query:
            return "UPDATE 1"
        return "UPDATE 0"


def _sweep(session_relay):
    async def _go():
        result = await session_relay.dispatch_queued_relays()
        await asyncio.gather(*list(session_relay._running), return_exceptions=True)
        return result

    return asyncio.run(_go())


def _relay_harness(monkeypatch, pool, deliver):
    from app.core import db_pool
    from app.services import session_relay

    gate_calls = []

    async def gate(goal_id, relay_id):
        gate_calls.append((goal_id, relay_id))
        return False

    async def idle(_sid):
        return False

    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    monkeypatch.setattr(session_relay, "_relay_paused", gate)
    monkeypatch.setattr(session_relay, "_deliver_answer", deliver)
    monkeypatch.setattr(session_relay, "_target_is_busy", idle)
    return session_relay, gate_calls


def test_completed_relay_reply_resumes_without_reasking(monkeypatch):
    deliveries = []

    async def deliver(origin, content, goal_id, relay_id):
        deliveries.append((origin, content, goal_id, relay_id))
        return True

    pool = _RelayPool(replies=[{"id": "relay", "origin": "origin", "goal_id": "goal",
                                "pending_reply": "completed answer"}])
    session_relay, gate_calls = _relay_harness(monkeypatch, pool, deliver)
    result = _sweep(session_relay)
    assert result["sent"] == 1
    assert deliveries == [("origin", "completed answer", "goal", "relay")]
    assert any("status='answered'" in q for q, _ in pool.writes)
    # 리뷰 지적 5 — 사이클이 회신마다 멈춤 조회를 따로 하지 않는다
    # (멈춘 목표는 SQL 에서 걸렀고, 배달 직전 재확인은 _deliver_answer 안의 한 번뿐).
    assert gate_calls == []
    reply_sql = next(q for q in pool.fetches if "r.status = 'blocked'" in q)
    assert "paused_at IS NOT NULL" in reply_sql and "DISTINCT ON (r.origin_session_id)" in reply_sql


def test_reply_resume_failure_does_not_abort_cycle(monkeypatch):
    """리뷰 지적 4 — 회신 배달이 예외를 내도 만료·대기열 배달은 계속 돈다."""
    async def boom(*_args):
        raise RuntimeError("stream broke")

    pool = _RelayPool(replies=[{"id": "relay-1", "origin": "origin", "goal_id": "goal",
                                "pending_reply": "answer"}])
    session_relay, _ = _relay_harness(monkeypatch, pool, boom)
    result = _sweep(session_relay)
    assert any("WHERE status = 'queued'" in q for q in pool.fetches)
    assert "expired" in result
    failed = [(q, a) for q, a in pool.writes if "status='failed'" in q and "reply_resume_failed" in str(a)]
    assert failed, pool.writes


def test_reply_repaused_after_claim_goes_back_to_blocked(monkeypatch):
    async def paused_again(*_args):
        return False

    pool = _RelayPool(replies=[{"id": "relay-1", "origin": "origin", "goal_id": "goal",
                                "pending_reply": "answer"}])
    session_relay, _ = _relay_harness(monkeypatch, pool, paused_again)
    _sweep(session_relay)
    assert any("status='blocked'" in q and "status='pending'" in q
               for q, _ in pool.writes if q.startswith("UPDATE session_relay SET status='blocked'"))
    assert not any("status='answered'" in q for q, _ in pool.writes)


def test_paused_goal_relay_does_not_block_other_relays_to_target(monkeypatch):
    """리뷰 지적 1 — 멈춘 목표의 질문은 SQL 에서 걸러 head-of-line 차단이 없다."""
    runs = []

    async def fake_run(relay_id, target, prompt, origin, question, goal_id=None):
        runs.append((relay_id, goal_id))

    async def deliver(*_args):
        return True

    pool = _RelayPool(queued=[{"id": "relay-free", "origin": "origin", "target": "target",
                               "question": "q", "goal_id": None}])
    session_relay, gate_calls = _relay_harness(monkeypatch, pool, deliver)
    monkeypatch.setattr(session_relay, "_run_relay", fake_run)
    result = _sweep(session_relay)
    assert runs == [("relay-free", None)] and result["sent"] == 1
    assert gate_calls == []
    queued_sql = next(q for q in pool.fetches if "WHERE status = 'queued'" in q)
    where = queued_sql[queued_sql.index("WHERE"):queued_sql.index("ORDER BY")]
    # 필터는 DISTINCT ON 보다 먼저(WHERE) 적용돼야 대상당 "멈추지 않은 것 중 가장 오래된" 것을 집는다.
    assert "NOT EXISTS" in where and "g.paused_at IS NOT NULL" in where
    assert "g.id = session_relay.goal_id" in where


def test_relay_expiry_only_exempts_currently_paused_goals(monkeypatch):
    """리뷰 지적 2·3 — 목표에 묶인 릴레이도 만료되고, 멈춘 동안만 미뤄지며, 상한이 있다."""
    async def deliver(*_args):
        return True

    pool = _RelayPool()
    session_relay, _ = _relay_harness(monkeypatch, pool, deliver)
    _sweep(session_relay)
    expiry, args = next((q, a) for q, a in pool.writes if "status='failed'" in q)
    assert "goal_id IS NULL" not in expiry
    assert "r.status IN ('queued', 'blocked')" in expiry
    assert "g.paused_at IS NOT NULL" in expiry
    assert "pending_reply=NULL" in expiry
    assert args == (session_relay._RELAY_QUEUE_MAX_AGE_HOURS,
                    session_relay._RELAY_PAUSED_MAX_AGE_HOURS)
    assert session_relay._RELAY_PAUSED_MAX_AGE_HOURS >= session_relay._RELAY_QUEUE_MAX_AGE_HOURS
    # 만료가 회신 재개·대기열 배달보다 먼저 돈다 — 해제 직후 묵은 회신이 쏟아지지 않는다.
    assert pool.events[0] == "expire", pool.events


def test_relay_goal_fk_does_not_block_goal_delete():
    """리뷰 지적 6 — 목표 삭제가 릴레이 FK 로 막히지 않는다."""
    root = Path(__file__).resolve().parents[2]
    sql = (root / "migrations/20260929_goal_pause_all_paths.sql").read_text()
    stmt = next(line_ for line_ in sql.split(";") if "session_relay ADD COLUMN IF NOT EXISTS goal_id" in line_)
    assert "ON DELETE SET NULL" in stmt


def test_dispatch_select_reads_columns_used_by_refund():
    """리뷰 지적 8 — 환급에 쓰는 두 칸을 후보 조회가 실제로 읽는다."""
    root = Path(__file__).resolve().parents[2]
    source = (root / "app/services/goal_dispatch.py").read_text()
    body = source[source.index("async def dispatch_pending_milestones"):]
    select = body[body.index("SELECT m.id::text AS milestone_id"):body.index("FROM milestones m")]
    assert "m.dispatched_at" in select and "m.dispatched_session_id" in select
    assert 'previous_dispatched_at=row["dispatched_at"]' in body


def test_system_trigger_sites_have_goal_pause_gate():
    root = Path(__file__).resolve().parents[2]
    automatic = {
        "app/services/goal_dispatch.py": {"_consume": "goal_paused"},
        "app/services/milestone_review.py": {"ask_pending_reviews": "goal_paused"},
        "app/services/session_relay.py": {
            "_run_relay": "_relay_paused",
            "_deliver_answer": "_relay_paused",
        },
    }
    manual = {("app/services/goal_intervene.py", "direct"),
              ("app/routers/goals.py", "confirm_milestone_api")}
    files = list((root / "app").rglob("*.py"))
    sites = []
    for path in files:
        source = path.read_text()
        rel = str(path.relative_to(root))
        tree = ast.parse(source)
        functions = [n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            if not any(k.arg == "intent_override" and isinstance(k.value, ast.Constant)
                       and k.value.value == "system_trigger" for k in call.keywords):
                continue
            enclosing = [n for n in functions if n.lineno <= call.lineno <= n.end_lineno]
            assert enclosing, (rel, call.lineno)
            node = min(enclosing, key=lambda n: n.end_lineno - n.lineno)
            own = ast.get_source_segment(source, node) or ""
            if (rel, node.name) in manual:
                sites.append((rel, node.name))
                continue
            assert node.name in automatic.get(rel, {}), (rel, node.name)
            gate = automatic[rel][node.name]
            if rel.endswith("goal_dispatch.py"):
                assert gate in source[source.index("async def dispatch_pending_milestones"):]
            else:
                assert gate in own, (rel, node.name)
            sites.append((rel, node.name))
    assert sites
