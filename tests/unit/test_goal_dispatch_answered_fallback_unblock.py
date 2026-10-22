"""답 있음/없음 구분 · 담당 없는 건의 주도 세션 대체 발송 · 막힘 자동 해제.

2026-10-22 실측. 막힌(`dispatch_blocked_at`) 마일스톤 36건 이상이 전부 "3회 보냈으나
답이 확인되지 않음" 이었다. 담당이 실제로 답한 건과 아예 답이 없는 건이 같은
문구였고, 담당 세션이 없는 건은 주도 세션이 있어도 한 번도 발송되지 않았으며,
막힌 뒤에 답이 와도 풀리지 않았다.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services import goal_dispatch

LEAD = "11111111-0000-0000-0000-000000000000"
OWNER = "22222222-0000-0000-0000-000000000000"


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.events.append((event, kw))

    warning = info
    error = info

    def named(self, event: str) -> list[dict]:
        return [kw for name, kw in self.events if name == event]


def _row(n: int = 1, **over) -> dict:
    now = datetime.now(UTC)
    row = {
        "milestone_id": f"{n:08d}-0000-0000-0000-000000000000",
        "milestone_title": f"M{n}",
        "description": "설명",
        "completion_criteria": "기준",
        "dispatch_count": 1,
        "dispatched_at": now - timedelta(hours=3),
        "dispatched_session_id": OWNER,
        "load_deferred_since": None,
        "dispatch_blocked_at": None,
        "goal_title": "목표",
        "project": "AADS",
        "goal_id": "g",
        "owner_role_key": "",
        "goal_lead_session_id": LEAD,
        "session_id": OWNER,
    }
    row.update(over)
    return row


class _Conn:
    def __init__(self, rows: list[dict], *, answers=(), live_link=False) -> None:
        self.rows = rows
        self.answers = list(answers)
        self.live_link = live_link
        self.executed: list[tuple[str, tuple]] = []
        self.claims: list[tuple] = []
        self.answer_queries: list[tuple] = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetch(self, *_a, **_k):
        return self.rows

    async def fetchval(self, query, *args):
        if "FROM goal_task_links" in query:
            return self.live_link
        if "role IN ('user', 'system')" in query:
            return args[1] + timedelta(minutes=1)
        if "FROM chat_messages" in query:
            self.answer_queries.append(args)
            return min((a for a in self.answers if a > args[1]), default=None)
        return None

    async def fetchrow(self, query, *args):
        assert "RETURNING dispatch_count" in query
        self.claims.append(args)
        return {"dispatch_count": 99}

    def updates(self, needle: str) -> list[tuple[str, tuple]]:
        return [(q, a) for q, a in self.executed if needle in q]


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


@pytest.fixture
def cycle(monkeypatch):
    from app.core import db_pool
    from app.services import orchestration_limits

    async def _free(*_a, **_k):
        return (False, "")

    async def _ok(*_a, **_k):
        return (True, "")

    async def _no_repair(_conn):
        return 0

    async def _columns(_conn):
        return {"link_state", "superseded_by"}

    monkeypatch.setattr(orchestration_limits, "owner_paused", _free)
    monkeypatch.setattr(orchestration_limits, "goal_paused", _free)
    monkeypatch.setattr(orchestration_limits, "cost_gate", _ok)
    monkeypatch.setattr(orchestration_limits, "load_gate", _ok)
    monkeypatch.setattr(goal_dispatch, "repair_owner_links", _no_repair)
    monkeypatch.setattr(goal_dispatch, "_ENABLED", True)
    monkeypatch.setattr(goal_dispatch, "_MAX_PER_CYCLE", 10)
    monkeypatch.setattr(goal_dispatch, "_MAX_DISPATCH", 3)
    monkeypatch.setattr(goal_dispatch, "_PROGRESS_GRACE_MIN", 45)
    monkeypatch.setattr(goal_dispatch.goal_manager, "link_optional_columns", _columns)
    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)
    spawned: list[dict] = []
    monkeypatch.setattr(goal_dispatch, "_spawn_send", lambda **kw: spawned.append(kw))

    def _run(rows, **conn_kw):
        conn = _Conn(rows, **conn_kw)
        monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
        result = asyncio.run(goal_dispatch.dispatch_pending_milestones())
        return result, conn, spawned, log

    return _run


def _ago(**kw) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


# ── A. 답 있음 / 없음 구분 ─────────────────────────────────────────────────

def test_answered_without_progress_resend_uses_distinct_message(cycle) -> None:
    result, conn, spawned, log = cycle([_row(dispatch_count=1)], answers=[_ago(minutes=100)])
    assert result["sent"] == 1
    message = spawned[0]["message"]
    assert "답은 확인됐으나 러너 제출이나 완료 보고가 없다" in message
    assert "막힌 이유를 보고" in message
    assert "답이 확인되지 않았다" not in message
    assert log.named("goal_dispatch_answer_without_progress")


def test_no_answer_resend_keeps_original_message(cycle) -> None:
    result, conn, spawned, log = cycle([_row(dispatch_count=1)], answers=[])
    assert result["sent"] == 1
    message = spawned[0]["message"]
    assert "답이 확인되지 않았다" in message
    assert "답은 확인됐으나" not in message
    assert not log.named("goal_dispatch_answer_without_progress")


def test_answered_without_progress_at_limit_blocks_with_distinct_note(cycle) -> None:
    result, conn, spawned, log = cycle([_row(dispatch_count=3)], answers=[_ago(minutes=100)])
    assert result["gave_up"] == 1 and result["sent"] == 0
    assert spawned == []
    (query, args), = conn.updates("dispatch_blocked_at = ")
    note = args[1]
    assert note.startswith("담당이 답했으나 진행(러너·완료 보고)이 없음 — 확인된 답 ")
    assert note.endswith(" KST")
    assert "답이 확인되지 않음" not in note
    assert log.named("goal_dispatch_blocked_answered_no_progress")


def test_no_answer_at_limit_keeps_original_note_and_limit(cycle) -> None:
    result, conn, spawned, log = cycle([_row(dispatch_count=3)], answers=[])
    assert result["gave_up"] == 1 and result["sent"] == 0
    (query, args), = conn.updates("dispatch_blocked_at = ")
    assert args[1] == "3회 보냈으나 답이 확인되지 않음 — 사람이 확인해야 한다"
    assert not log.named("goal_dispatch_blocked_answered_no_progress")


# ── B. 담당 없음 → 주도 세션 대체 발송 ──────────────────────────────────────

def test_missing_owner_with_lead_sends_to_lead_and_records_it(cycle) -> None:
    row = _row(session_id=None, dispatched_at=None, dispatched_session_id=None,
               dispatch_count=0, owner_role_key="")
    result, conn, spawned, log = cycle([row])
    assert result["sent"] == 1
    assert spawned[0]["session_id"] == LEAD
    (claim,) = conn.claims
    assert claim[1] == LEAD, "dispatched_session_id 에 실제 보낸 주도 세션이 기록돼야 한다"
    notes = [a[1] for _q, a in conn.updates("dispatch_note = $2, updated_at")]
    assert notes == ["담당 세션 없음 — 목표 주도 세션으로 대체 발송"]
    assert "담당 세션 없음 — 목표 주도 세션으로 대체 발송" in spawned[0]["message"]


def test_missing_owner_with_role_still_requests_owner_session(cycle, monkeypatch) -> None:
    from app.services import owner_session_provision

    requests: list[dict] = []

    async def _request(**kw):
        requests.append(kw)
        return {"request_id": "r" * 8}

    monkeypatch.setattr(owner_session_provision, "request_owner_session", _request)
    row = _row(session_id=None, dispatched_at=None, dispatched_session_id=None,
               dispatch_count=0, owner_role_key="ops")
    result, conn, spawned, log = cycle([row])
    assert result["sent"] == 1 and result["owner_requests"] == 1
    assert requests and requests[0]["role_key"] == "ops"
    assert spawned[0]["session_id"] == LEAD
    notes = [a[1] for _q, a in conn.updates("dispatch_note = $2, updated_at")]
    assert notes == ["담당 세션 없음 — 목표 주도 세션으로 대체 발송"], (
        "대체 발송 건에서 승인 요청 쪽이 dispatch_note 를 덮어쓰면 안 된다"
    )


def test_missing_owner_without_lead_sends_nothing(cycle) -> None:
    row = _row(session_id=None, dispatched_at=None, dispatched_session_id=None,
               dispatch_count=0, owner_role_key="", goal_lead_session_id="")
    result, conn, spawned, log = cycle([row])
    assert result["sent"] == 0 and result["skipped"] == 1
    assert spawned == [] and conn.claims == []
    assert any(a[1] == "담당 역할이 지정되지 않음" for _q, a in conn.updates("dispatch_note = $2"))


def test_substitute_send_respects_one_session_per_cycle(cycle) -> None:
    rows = [
        _row(1, session_id=None, dispatched_at=None, dispatched_session_id=None, dispatch_count=0),
        _row(2, session_id=None, dispatched_at=None, dispatched_session_id=None, dispatch_count=0),
    ]
    result, conn, spawned, log = cycle(rows)
    assert result["sent"] == 1 and result["skipped"] == 1
    assert [s["session_id"] for s in spawned] == [LEAD]


def test_substitute_send_respects_per_cycle_cap(cycle, monkeypatch) -> None:
    monkeypatch.setattr(goal_dispatch, "_MAX_PER_CYCLE", 0)
    row = _row(session_id=None, dispatched_at=None, dispatched_session_id=None, dispatch_count=0)
    result, conn, spawned, log = cycle([row])
    assert result["sent"] == 0 and spawned == [] and conn.claims == []


def test_substitute_send_respects_cost_gate(cycle, monkeypatch) -> None:
    from app.services import orchestration_limits

    async def _closed(*_a, **_k):
        return (False, "cap")

    monkeypatch.setattr(orchestration_limits, "cost_gate", _closed)
    row = _row(session_id=None, dispatched_at=None, dispatched_session_id=None, dispatch_count=0)
    result, conn, spawned, log = cycle([row])
    assert result["sent"] == 0 and spawned == [] and conn.claims == []


# ── C. 막힘 자동 해제 ──────────────────────────────────────────────────────

def _blocked(**over) -> dict:
    return _row(dispatch_count=3, dispatch_blocked_at=_ago(hours=1), **over)


def test_answer_after_block_releases_resets_count_and_logs(cycle) -> None:
    result, conn, spawned, log = cycle([_blocked()], answers=[_ago(minutes=20)])
    assert result["skipped"] == 1 and result["gave_up"] == 0 and result["sent"] == 0
    assert spawned == []
    (query, args), = conn.updates("dispatch_count = 0")
    assert "dispatch_blocked_at = NULL" in query and "dispatch_note = NULL" in query
    assert args[0] == _blocked()["milestone_id"]
    (reset,) = log.named("goal_dispatch_record_reset")
    assert reset["where"] == "answered_after_block"
    assert reset["prev_dispatch_count"] == 3
    assert reset["prev_dispatched_at"]
    assert log.named("goal_dispatch_block_released")[0]["reason"] == "answered_after_block"


def test_answer_query_for_release_starts_at_the_block_time(cycle) -> None:
    row = _blocked()
    _result, conn, _spawned, _log = cycle([row], answers=[_ago(minutes=20)])
    assert conn.answer_queries[0][1] == row["dispatch_blocked_at"]


def test_answer_before_block_does_not_release(cycle) -> None:
    result, conn, spawned, log = cycle([_blocked()], answers=[_ago(minutes=150)])
    assert not conn.updates("dispatch_count = 0")
    assert not log.named("goal_dispatch_record_reset")
    assert result["gave_up"] == 1 and spawned == []
    (query, args), = conn.updates("dispatch_blocked_at = ")
    assert args[1].startswith("담당이 답했으나 진행")


def test_runner_link_after_block_releases_without_resetting_count(cycle) -> None:
    result, conn, spawned, log = cycle([_blocked()], live_link=True)
    assert result["skipped"] == 1 and spawned == []
    assert conn.updates("dispatch_blocked_at = NULL, dispatch_note = NULL")
    assert not conn.updates("dispatch_count = 0")
    assert not log.named("goal_dispatch_record_reset")
    assert log.named("goal_dispatch_block_released")[0]["reason"] == "runner_progress"


def test_release_checks_the_substitute_lead_session(cycle) -> None:
    row = _blocked(session_id=None, dispatched_session_id=LEAD)
    result, conn, spawned, log = cycle([row], answers=[_ago(minutes=20)])
    assert result["skipped"] == 1
    assert conn.answer_queries[0][0] == LEAD
    assert log.named("goal_dispatch_record_reset")


def test_unblocked_milestone_never_runs_release_path(cycle) -> None:
    result, conn, spawned, log = cycle([_row(dispatch_count=1)], answers=[])
    assert not log.named("goal_dispatch_block_released")
    assert not conn.updates("dispatch_count = 0")


def test_report_done_clears_block_markers(monkeypatch) -> None:
    from app.core import db_pool
    from app.services import milestone_review

    class _ReportConn:
        def __init__(self) -> None:
            self.executed: list[str] = []

        async def fetchrow(self, *_a):
            return {"status": "in_progress", "title": "제목", "completion_criteria": "",
                    "goal_id": "g"}

        async def execute(self, query, *_a):
            self.executed.append(query)
            return "UPDATE 1"

    conn = _ReportConn()
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
    out = asyncio.run(milestone_review.report_done(
        "00000001-0000-0000-0000-000000000000",
        {"summary": "무엇을 했고 결과가 이렇다는 스무 자 이상의 요약입니다"}, OWNER,
    ))
    assert out["status"] == "review"
    (query,) = conn.executed
    assert "dispatch_blocked_at = NULL" in query and "dispatch_note = NULL" in query
