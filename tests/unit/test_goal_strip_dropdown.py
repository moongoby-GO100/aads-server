"""목표띠 드롭다운 — for-session 진행 우선순 그룹(strip_group·strip_order·last_activity_at).

설계 명세 goal-strip-dropdown-spec rev1 2절. DB 는 쓰지 않고 커넥션만 대역으로 바꾼다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services import goal_activity as ga

NOW = datetime.now(timezone.utc)
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "22222222-2222-4222-8222-222222222222"
_CTX = {"tenant": {"id": TENANT}, "user": {"user_id": "u1"}}


def _group(activity=None, priority="P1", dormant=False, *, paused=False, status="active", note=None):
    return ga.strip_group(
        activity, priority, dormant, paused=paused, status=status, dispatch_note=note,
    )


# ── strip_group 7개 분기 ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"activity": "working"}, "working"),
        ({"activity": "awaiting"}, "awaiting"),
        ({"status": "blocked"}, "blocked"),
        ({"priority": "P0"}, "p0"),
        ({"priority": "P0-CRITICAL"}, "p0"),
        ({"priority": "P2"}, "other"),
        ({"paused": True}, "paused"),
        ({"dormant": True}, "dormant"),
    ],
)
def test_strip_group_branches(kwargs, expected):
    assert _group(**kwargs) == expected


def test_strip_group_covers_all_seven_groups():
    seen = {
        _group("working"), _group("awaiting"), _group(status="blocked"), _group(priority="P0"),
        _group(), _group(paused=True), _group(dormant=True),
    }
    assert seen == set(ga.STRIP_GROUP_ORDER)


def test_dispatch_note_text_means_blocked():
    assert _group(note="막힘 사유") == "blocked"


@pytest.mark.parametrize("note", ["", "   ", "\n\t", None])
def test_blank_dispatch_note_is_not_blocked(note):
    assert _group(note=note) == "other"


def test_non_string_dispatch_note_is_not_blocked():
    assert _group(note=123) == "other"


# ── 충돌 ────────────────────────────────────────────────────────────────────
def test_paused_beats_working():
    assert _group("working", paused=True) == "paused"


def test_dormant_beats_blocked():
    assert _group(dormant=True, status="blocked", note="막힘") == "dormant"


def test_draft_is_dormant():
    assert _group(status="draft") == "dormant"


def test_working_beats_dormant_and_blocked():
    assert _group("working", dormant=True, status="blocked") == "working"


def test_blocked_beats_p0():
    assert _group(priority="P0", status="blocked") == "blocked"


def test_strip_order_matches_group_order():
    assert [ga.strip_order(g) for g in ga.STRIP_GROUP_ORDER] == list(range(7))
    assert ga.STRIP_GROUP_ORDER == (
        "working", "awaiting", "blocked", "p0", "other", "paused", "dormant",
    )


# ── dropdown_sort_key ───────────────────────────────────────────────────────
def _item(title, priority="P1", group="other", last=None):
    return {
        "title": title, "priority": priority, "strip_order": ga.strip_order(group),
        "last_activity_at": last.isoformat() if last else None,
    }


def _titles(items):
    return [i["title"] for i in sorted(items, key=ga.dropdown_sort_key)]


def test_group_order_dominates_priority():
    items = [
        _item("휴면", "P0-CRITICAL", "dormant"),
        _item("기타", "P3", "other"),
        _item("작업", "P3", "working"),
    ]
    assert _titles(items) == ["작업", "기타", "휴면"]


def test_priority_order_within_group():
    items = [_item("c", "P1"), _item("b", "P0"), _item("a", "P0-CRITICAL")]
    assert _titles(items) == ["a", "b", "c"]


def test_recent_activity_first_within_same_priority():
    items = [
        _item("오래됨", last=NOW - timedelta(days=3)),
        _item("최근", last=NOW - timedelta(minutes=1)),
        _item("중간", last=NOW - timedelta(hours=5)),
    ]
    assert _titles(items) == ["최근", "중간", "오래됨"]


def test_no_activity_goes_last_then_title():
    items = [
        _item("가", last=None),
        _item("나", last=NOW - timedelta(days=30)),
        _item("다", last=None),
    ]
    assert _titles(items) == ["나", "가", "다"]


def test_unparseable_activity_treated_as_none():
    bad = {**_item("나"), "last_activity_at": "not-a-date"}
    assert _titles([bad, _item("가", last=NOW)]) == ["가", "나"]


def test_naive_and_aware_timestamps_compare():
    naive = _item("naive", last=NOW.replace(tzinfo=None))
    older = _item("older", last=NOW - timedelta(hours=1))
    assert _titles([older, naive]) == ["naive", "older"]


def test_existing_strip_helpers_unchanged():
    assert ga.strip_rank("working", "P3", True) == ga.RANK_WORKING
    assert ga.strip_rank(None, "P0", False) == ga.RANK_P0
    assert ga.strip_rank(None, "P1", True) == ga.RANK_DORMANT
    items = [
        {"title": "b", "priority": "P1", "strip_rank": ga.RANK_OTHER},
        {"title": "a", "priority": "P0", "strip_rank": ga.RANK_P0},
    ]
    assert [i["title"] for i in ga.sort_for_strip(items)] == ["a", "b"]


# ── GET /goals/for-session 응답 ─────────────────────────────────────────────
class _Conn:
    def __init__(self, session_rows, activity_rows, job_states):
        self.session_rows, self.activity_rows, self.job_states = (
            session_rows, activity_rows, job_states,
        )

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
        if "FROM pipeline_jobs j" in query and "goal_id IS NULL" in query:
            return 0
        return None

    async def fetch(self, query, *args):
        if "FROM goal_task_links l" in query and "JOIN chat_sessions s" in query:
            return self.session_rows
        if "ANY($2::text[])" in query and "pipeline_jobs j" in query:
            return self.job_states
        if "WITH g AS" in query:
            return self.activity_rows
        return []


def _gid(n):
    return f"11111111-1111-4111-8111-{n:012d}"


def _row(n, title, priority, *, status="active", paused=False, note=None, reason=None):
    return {
        "id": _gid(n), "goal_id": _gid(n), "project": "AADS", "title": title,
        "priority": priority, "status": status, "progress": 0,
        "paused_at": NOW if paused else None,
        "paused_reason": "사유" if paused else reason, "approval_policy": {},
        "is_lead": True, "role_key": "cto", "milestone_id": None, "milestone": None,
        "dispatch_note": note, "dispatch_blocked_at": None,
    }


@pytest.fixture
def for_session(monkeypatch):
    import app.core.db_pool as db_pool
    import app.services.direction_guard as direction_guard
    from app.routers import goals as goals_router

    async def halted():
        return False

    monkeypatch.setattr(direction_guard, "is_halted", halted)

    def run(session_rows, activity_rows, job_states=()):
        conn = _Conn(session_rows, activity_rows, list(job_states))
        monkeypatch.setattr(db_pool, "get_pool", lambda: conn)
        return asyncio.run(goals_router.goals_for_session(SESSION, _CTX))

    return run


def test_for_session_adds_group_fields_and_keeps_strip_rank(for_session):
    rows = [
        _row(1, "휴면", "P0"),
        _row(2, "멈춤", "P0", paused=True),
        _row(3, "기타", "P2"),
        _row(4, "P0 목표", "P0"),
        _row(5, "막힘", "P2", status="blocked", reason="외부 승인 대기"),
        _row(6, "승인 대기", "P2"),
        _row(7, "작업 중", "P2"),
    ]
    activity = [
        {"goal_id": _gid(1), "last_activity_at": NOW - timedelta(days=30), "block_note": None},
        *[
            {"goal_id": _gid(n), "last_activity_at": NOW - timedelta(minutes=n), "block_note": None}
            for n in range(2, 8)
        ],
    ]
    jobs = [
        {"goal_id": _gid(7), "milestone_id": None, "job_id": "runner-1", "status": "running", "created_at": NOW},
        {"goal_id": _gid(6), "milestone_id": None, "job_id": "runner-2", "status": "awaiting_approval", "created_at": NOW},
    ]
    out = for_session(rows, activity, jobs)
    goals = out["goals"]
    assert [g["title"] for g in goals] == ["작업 중", "승인 대기", "막힘", "P0 목표", "기타", "멈춤", "휴면"]
    assert [g["strip_group"] for g in goals] == list(ga.STRIP_GROUP_ORDER)
    assert [g["strip_order"] for g in goals] == list(range(7))
    by_title = {g["title"]: g for g in goals}
    assert by_title["작업 중"]["last_activity_at"] == (NOW - timedelta(minutes=7)).isoformat()
    # 기존 strip_rank 값은 그대로다(멈춤은 활동·휴면이 없으므로 P0 순번).
    assert by_title["작업 중"]["strip_rank"] == ga.RANK_WORKING
    assert by_title["승인 대기"]["strip_rank"] == ga.RANK_AWAITING
    assert by_title["멈춤"]["strip_rank"] == ga.RANK_P0
    assert by_title["막힘"]["strip_rank"] == ga.RANK_OTHER
    assert by_title["휴면"]["strip_rank"] == ga.RANK_DORMANT
    assert by_title["멈춤"]["goal_paused"] is True


def test_for_session_last_activity_null_when_unknown_and_sorted_last(for_session):
    rows = [_row(1, "활동 없음", "P1"), _row(2, "활동 있음", "P1")]
    activity = [{"goal_id": _gid(2), "last_activity_at": NOW - timedelta(hours=1), "block_note": None}]
    out = for_session(rows, activity)
    assert [g["title"] for g in out["goals"]] == ["활동 있음", "활동 없음"]
    assert out["goals"][1]["last_activity_at"] is None
    assert all("last_activity_at" in g for g in out["goals"])


def test_for_session_blank_dispatch_note_is_not_blocked(for_session):
    rows = [_row(1, "공백", "P1", note="  "), _row(2, "사유", "P1", note="대기 중")]
    activity = [
        {"goal_id": _gid(1), "last_activity_at": NOW, "block_note": None},
        {"goal_id": _gid(2), "last_activity_at": NOW, "block_note": None},
    ]
    out = for_session(rows, activity)
    groups = {g["title"]: g["strip_group"] for g in out["goals"]}
    assert groups == {"공백": "other", "사유": "blocked"}
