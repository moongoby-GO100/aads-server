"""목표·마일스톤 진행 상태(progress_state)와 마일스톤 다시 보내기 API.

DB 는 쓰지 않고 커넥션만 대역으로 바꾼다.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.routers import goals as goals_router
from app.services import goal_dispatch, goal_manager
from app.services import goal_progress_state as gps

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_TENANT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GOAL = "11111111-1111-4111-8111-111111111111"
MS = "33333333-3333-4333-8333-333333333333"
SESSION = "22222222-2222-4222-8222-222222222222"
LEAD = "44444444-4444-4444-8444-444444444444"
_CTX = {"tenant": {"id": TENANT}, "user": {"user_id": "u1"}}


def _ms(**over):
    base = {
        "milestone_id": MS, "goal_id": GOAL, "title": "M1",
        "dispatched_at": None, "dispatch_count": 0, "dispatch_blocked_at": None,
        "dispatch_note": None, "dispatched_session_id": None,
        "goal_lead_session_id": "", "owner_resolved": True, "owner_paused": False,
        "live_runner": False, "answered_at": None,
    }
    base.update(over)
    return base


def _state(goal_paused=False, **over):
    return gps.classify_milestone(_ms(**over), goal_paused=goal_paused, now=NOW)


# ── 상태 7가지 ───────────────────────────────────────────────────────────────
def test_working_with_live_runner_even_if_block_record_is_stale():
    got = _state(live_runner=True, dispatch_blocked_at=NOW, dispatched_at=NOW)
    assert got["state"] == gps.WORKING


def test_working_when_owner_answered_within_grace():
    got = _state(
        dispatched_at=NOW - timedelta(minutes=20), dispatch_count=1,
        answered_at=NOW - timedelta(minutes=5),
    )
    assert got["state"] == gps.WORKING


def test_no_owner_without_lead_is_not_substitute():
    got = _state(owner_resolved=False, dispatch_note="담당 역할이 지정되지 않음")
    assert got["state"] == gps.NO_OWNER
    assert got["substitute"] is False
    assert "담당 역할" in got["reason"]


def test_no_owner_with_substitute_dispatch_marks_substitute():
    got = _state(
        owner_resolved=False, goal_lead_session_id=LEAD, dispatched_at=NOW,
        dispatch_count=1, dispatched_session_id=LEAD,
    )
    assert got["state"] == gps.NO_OWNER
    assert got["substitute"] is True


def test_no_owner_substitute_recognised_by_dispatch_marker_note():
    got = _state(
        owner_resolved=False, goal_lead_session_id=LEAD,
        dispatch_note=goal_dispatch._SUBSTITUTE_NOTE,
    )
    assert got["substitute"] is True


def test_awaiting_reply_when_sent_and_no_answer_yet():
    got = _state(dispatched_at=NOW - timedelta(minutes=10), dispatch_count=1)
    assert got["state"] == gps.AWAITING_REPLY
    assert "1회" in got["reason"]


def test_answered_no_progress_after_grace_without_block_record():
    got = _state(
        dispatched_at=NOW - timedelta(hours=2), dispatch_count=1,
        answered_at=NOW - timedelta(hours=1),
    )
    assert got["state"] == gps.ANSWERED_NO_PROGRESS


def test_answered_no_progress_when_blocked_with_dispatch_marker_note():
    got = _state(
        dispatch_blocked_at=NOW, dispatched_at=NOW, dispatch_count=3,
        dispatch_note=f"{gps.ANSWERED_NO_PROGRESS_MARK}이 없음 — 확인된 답 20:00 KST",
    )
    assert got["state"] == gps.ANSWERED_NO_PROGRESS


def test_blocked_when_blocked_at_set_and_no_answer():
    got = _state(
        dispatch_blocked_at=NOW, dispatched_at=NOW, dispatch_count=3,
        dispatch_note="3회 보냈으나 답이 확인되지 않음 — 사람이 확인해야 한다",
    )
    assert got["state"] == gps.BLOCKED
    assert "사람이 확인" in got["reason"]


def test_paused_by_goal_wins_over_everything():
    got = _state(goal_paused=True, live_runner=True, dispatch_blocked_at=NOW)
    assert got["state"] == gps.PAUSED


def test_paused_by_owner():
    assert _state(owner_paused=True, dispatched_at=NOW)["state"] == gps.PAUSED


def test_idle_when_nothing_sent_yet():
    assert _state()["state"] == gps.IDLE


def test_dispatch_marker_strings_still_exist_in_goal_dispatch():
    src = inspect.getsource(goal_dispatch)
    assert gps.ANSWERED_NO_PROGRESS_MARK in src
    assert gps.SUBSTITUTE_NOTE == goal_dispatch._SUBSTITUTE_NOTE


# ── 목표 대표값 ──────────────────────────────────────────────────────────────
def _classified(state, title="M", **over):
    return {
        "milestone_id": f"id-{title}", "title": title, "state": state,
        "reason": f"{state}-reason", "substitute": False, **over,
    }


def test_goal_representative_follows_severity_order():
    order = [
        gps.BLOCKED, gps.ANSWERED_NO_PROGRESS, gps.NO_OWNER, gps.AWAITING_REPLY,
        gps.WORKING, gps.IDLE,
    ]
    for i, worst in enumerate(order):
        pool = [_classified(s, title=s) for s in reversed(order[i:])]
        got = gps.summarize_goal(pool)
        assert got["progress_state"] == worst
        assert got["progress_reason"].startswith(f"{worst}:")


def test_goal_pause_takes_priority_over_blocked():
    got = gps.summarize_goal(
        [_classified(gps.BLOCKED, dispatch_blocked_at=NOW)],
        goal_paused=True, goal_paused_reason="점검",
    )
    assert got["progress_state"] == gps.PAUSED
    assert "점검" in got["progress_reason"]


def test_goal_without_in_progress_milestones_is_idle():
    assert gps.summarize_goal([])["progress_state"] == gps.IDLE


def test_goal_lists_blocked_milestones_with_reason_and_time():
    blocked = _classified(
        gps.BLOCKED, title="막힌것", dispatch_blocked_at=NOW, dispatch_note="사유 텍스트",
    )
    got = gps.summarize_goal([blocked, _classified(gps.WORKING, title="진행")])
    assert got["blocked_milestones"] == [{
        "milestone_id": "id-막힌것", "title": "막힌것", "state": gps.BLOCKED,
        "reason": "사유 텍스트", "blocked_at": NOW.isoformat(),
    }]


def test_goal_substitute_flag_follows_representative():
    got = gps.summarize_goal([_classified(gps.NO_OWNER, substitute=True)])
    assert got["progress_substitute"] is True


# ── progress_for_goals / for-session 응답 ───────────────────────────────────
class _Conn:
    def __init__(self, session_rows=(), goal_rows=(), ms_rows=()):
        self.session_rows, self.goal_rows, self.ms_rows = session_rows, goal_rows, ms_rows
        self.progress_calls: list[tuple] = []

    def acquire(self):
        conn = self

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def fetchval(self, query, *args):
        if "FROM chat_sessions WHERE id" in query:
            return 1
        if "goal_id IS NULL" in query:
            return 0
        return None

    async def fetch(self, query, *args):
        if "FROM goals WHERE id = ANY" in query:
            self.progress_calls.append(args)
            return list(self.goal_rows)
        if "FROM milestones m" in query and "live_runner" in query:
            return list(self.ms_rows)
        if "FROM goal_task_links l" in query and "JOIN chat_sessions s" in query:
            return list(self.session_rows)
        return []


@pytest.fixture(autouse=True)
def _no_link_probe(monkeypatch):
    async def columns(conn):
        return set()

    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)


def test_progress_for_goals_builds_goal_and_milestone_values():
    conn = _Conn(
        goal_rows=[{"goal_id": GOAL, "paused_at": None, "paused_reason": None}],
        ms_rows=[
            _ms(title="진행", milestone_id="m-work", live_runner=True),
            _ms(
                title="막힘", milestone_id="m-block", dispatch_blocked_at=NOW,
                dispatched_at=NOW, dispatch_count=3, dispatch_note="사유",
            ),
        ],
    )
    out = asyncio.run(gps.progress_for_goals(conn, [GOAL], TENANT, now=NOW))[GOAL]
    assert out["progress_state"] == gps.BLOCKED
    assert out["milestones"]["m-work"]["progress_state"] == gps.WORKING
    assert out["milestones"]["m-block"]["progress_state"] == gps.BLOCKED
    assert [b["milestone_id"] for b in out["blocked_milestones"]] == ["m-block"]
    assert conn.progress_calls[0][1] == TENANT


def test_progress_for_goals_degrades_to_empty_on_query_failure():
    class _Broken(_Conn):
        async def fetch(self, query, *args):
            raise RuntimeError("db down")

    out = asyncio.run(gps.progress_for_goals(_Broken(), [GOAL], TENANT))
    assert out[GOAL]["progress_state"] is None
    assert out[GOAL]["blocked_milestones"] == []


def test_for_session_keeps_existing_fields_and_adds_progress_state(monkeypatch):
    import app.core.db_pool as db_pool
    import app.services.direction_guard as direction_guard

    async def halted():
        return False

    monkeypatch.setattr(direction_guard, "is_halted", halted)
    session_row = {
        "id": GOAL, "goal_id": GOAL, "project": "AADS", "title": "목표",
        "priority": "P1", "status": "active", "progress": 0, "paused_at": None,
        "paused_reason": None, "approval_policy": {}, "is_lead": True,
        "role_key": "cto", "milestone_id": MS, "milestone": "M1",
        "dispatch_note": "담당 세션 없음", "dispatch_blocked_at": None,
    }
    conn = _Conn(
        session_rows=[session_row],
        goal_rows=[{"goal_id": GOAL, "paused_at": None, "paused_reason": None}],
        ms_rows=[_ms(owner_resolved=False, goal_lead_session_id=LEAD, dispatched_session_id=LEAD,
                     dispatched_at=NOW, dispatch_count=1)],
    )
    monkeypatch.setattr(db_pool, "get_pool", lambda: conn)

    out = asyncio.run(goals_router.goals_for_session(SESSION, _CTX))

    assert set(out) >= {"halted", "goals", "unlinked_count"}
    goal = out["goals"][0]
    for key in (
        "goal_id", "title", "status", "project", "priority", "progress", "is_lead",
        "role_key", "milestone_id", "milestone", "dispatch_note", "dispatch_blocked_at",
        "goal_paused", "goal_paused_reason", "priority_rank", "dormant", "dormant_reason",
        "activity", "current_runner", "strip_rank", "strip_group", "strip_order",
        "last_activity_at",
    ):
        assert key in goal, key
    assert goal["dispatch_note"] == "담당 세션 없음"
    assert goal["progress_state"] == gps.NO_OWNER
    assert goal["progress_substitute"] is True
    assert goal["progress_reason"]
    assert goal["blocked_milestones"] == []


# ── POST /goals/milestones/{id}/redispatch ──────────────────────────────────
class _RedispatchConn:
    def __init__(self, ms_row):
        self.ms_row = ms_row
        self.executed: list[tuple] = []
        self.select_args: tuple = ()

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        assert "FROM milestones m" in query and "m.tenant_id = $2::uuid" in query
        self.select_args = args
        row = self.ms_row
        if row is None or args[1] != row["tenant"]:
            return None
        return row

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


class _Log:
    def __init__(self):
        self.events = []

    def info(self, event, **kw):
        self.events.append((event, kw))

    warning = info


def _redispatch_row(**over):
    row = {
        "tenant": TENANT, "status": "in_progress", "goal_id": GOAL,
        "dispatched_at": NOW, "dispatch_count": 3, "goal_paused": False,
        "has_target": True,
    }
    row.update(over)
    return row


def _run_redispatch(monkeypatch, conn, tenant=TENANT):
    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: conn)
    return asyncio.run(goals_router.redispatch_milestone(
        gps_uuid(MS), {"tenant": {"id": tenant}, "user": {"user_id": "u1"}},
    ))


def gps_uuid(value):
    from uuid import UUID

    return UUID(value)


def test_redispatch_clears_dispatch_record_and_traces_previous_values(monkeypatch):
    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)
    conn = _RedispatchConn(_redispatch_row())

    out = _run_redispatch(monkeypatch, conn)

    assert out["redispatched"] is True and out["milestone_id"] == MS
    (query, args), = conn.executed
    for fragment in (
        "dispatched_at = NULL", "dispatch_count = 0", "dispatch_note = NULL",
        "dispatch_blocked_at = NULL", "status = 'in_progress'", "tenant_id = $2::uuid",
    ):
        assert fragment in query
    assert args == (MS, TENANT)
    (_, trace), = [e for e in log.events if e[0] == "goal_dispatch_record_reset"]
    assert trace["where"] == "app/routers/goals.py:redispatch_milestone"
    assert trace["prev_dispatch_count"] == 3
    assert trace["prev_dispatched_at"] == NOW.isoformat()
    assert trace["milestone"] == MS[:8]


def test_redispatch_other_tenant_is_404_and_writes_nothing(monkeypatch):
    conn = _RedispatchConn(_redispatch_row())
    with pytest.raises(HTTPException) as exc:
        _run_redispatch(monkeypatch, conn, tenant=OTHER_TENANT)
    assert exc.value.status_code == 404
    assert conn.executed == []


def test_redispatch_without_target_is_409_no_dispatch_target(monkeypatch):
    conn = _RedispatchConn(_redispatch_row(has_target=False))
    with pytest.raises(HTTPException) as exc:
        _run_redispatch(monkeypatch, conn)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "no_dispatch_target"
    assert conn.executed == []


def test_redispatch_only_for_in_progress_milestone(monkeypatch):
    conn = _RedispatchConn(_redispatch_row(status="review"))
    with pytest.raises(HTTPException) as exc:
        _run_redispatch(monkeypatch, conn)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "milestone_not_in_progress"
    assert conn.executed == []


def test_redispatch_route_requires_member_role():
    route = next(
        r for r in goals_router.router.routes
        if getattr(r, "path", "") == "/goals/milestones/{milestone_id}/redispatch"
    )
    assert "POST" in route.methods
    deps = [d.call for d in route.dependant.dependencies]
    assert goals_router.require_tenant_member in deps or any(
        getattr(d, "__name__", "") == getattr(goals_router.require_tenant_member, "__name__", "")
        for d in deps
    )
