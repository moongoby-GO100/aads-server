"""목표 지시서 바인딩과 답변 후 실제 진행 여부를 검증한다."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.services import goal_dispatch, pipeline_runner_service

GOAL_ID = "1997c471-ba2e-4f0d-a1e3-9df0dd6d1240"
MILESTONE_ID = "05fdc2bb-0000-0000-0000-000000000000"
SESSION_ID = "15782f6e-0000-0000-0000-000000000000"


class _Log:
    """dispatch 경로가 남긴 warning 이벤트만 모으는 최소 로거."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def warning(self, event, **_fields) -> None:
        self.events.append(event)

    def info(self, *_args, **_kwargs) -> None:
        pass


def _patch_dispatch_gates(monkeypatch, conn) -> None:
    """두 테스트가 똑같이 쓰던 게이트/의존 패치를 한 곳으로 모은다."""
    from app.services import orchestration_limits

    async def _open(*_args):
        return True, ""

    async def _not_paused(*_args):
        return False, ""

    async def _no_repair(_conn):
        return 0

    async def _columns(_conn):
        return conn.columns

    monkeypatch.setattr(orchestration_limits, "owner_paused", _not_paused)
    monkeypatch.setattr(orchestration_limits, "goal_paused", _not_paused)
    monkeypatch.setattr(orchestration_limits, "cost_gate", _open)
    monkeypatch.setattr(orchestration_limits, "load_gate", _open)
    monkeypatch.setattr(goal_dispatch, "repair_owner_links", _no_repair)
    monkeypatch.setattr(goal_dispatch, "_ENABLED", True)
    monkeypatch.setattr(goal_dispatch, "_spawn_send", lambda **kwargs: None)
    monkeypatch.setattr(goal_dispatch.goal_manager, "link_optional_columns", _columns)



def _row() -> dict:
    return {
        "goal_id": GOAL_ID,
        "milestone_id": MILESTONE_ID,
        "goal_title": "목표",
        "milestone_title": "마일스톤",
        "description": "설명",
        "completion_criteria": "완료 기준",
        "dispatch_count": 1,
        "dispatched_at": datetime.now(UTC) - timedelta(hours=2),
        "dispatched_session_id": SESSION_ID,
        "dispatch_blocked_at": None,
        "load_deferred_since": None,
        "session_id": SESSION_ID,
        "project": "AADS",
    }


def test_message_contains_line_start_binding_tags() -> None:
    message = goal_dispatch._build_message(_row())
    assert re.search(rf"^GOAL_ID: {GOAL_ID}$", message, re.MULTILINE)
    assert re.search(rf"^MILESTONE_ID: {MILESTONE_ID}$", message, re.MULTILINE)
    assert "이 두 줄을 지시서에 그대로 넣어라" in message


def test_message_binding_round_trips_through_runner_parser() -> None:
    binding = pipeline_runner_service.parse_goal_binding(goal_dispatch._build_message(_row()))
    assert binding.goal_id == GOAL_ID
    assert binding.milestone_id == MILESTONE_ID


class _Conn:
    def __init__(self, answers: list[datetime], links: list[dict], columns: set[str]) -> None:
        self.answers = answers
        self.links = links
        self.columns = columns
        self.claims = 0
        self.link_queries: list[str] = []
        self.anchor_missing = False

    async def execute(self, *_args):
        return "INSERT 0 0"

    async def fetch(self, *_args):
        return [_row()]

    async def fetchval(self, query, *args):
        if "FROM chat_messages" in query:
            if "role IN ('user', 'system')" in query:
                assert args[0] == SESSION_ID and isinstance(args[1], datetime)
                return None if self.anchor_missing else args[1] + timedelta(minutes=1)
            assert "length(content) > 40" in query
            assert "content NOT LIKE '⏳%'" in query
            assert "content NOT LIKE '⚠️ _응답 생성이%'" in query
            assert "MIN(created_at)" in query
            return min((answer for answer in self.answers if answer and answer > args[1]), default=None)
        if "FROM goal_task_links" in query:
            assert args == (MILESTONE_ID,)
            assert "task_type = 'pipeline_job'" in query
            assert "status NOT IN ('failed', 'error', 'rejected', 'rejected_done')" in query
            assert ("COALESCE(link_state, 'active') = 'active'" in query) == ("link_state" in self.columns)
            assert ("superseded_by IS NULL" in query) == ("superseded_by" in self.columns)
            assert "FROM pipeline_jobs j" in query and "'cancelled'" in query
            self.link_queries.append(query)
            return any(
                ("link_state" not in self.columns or link.get("link_state", "active") == "active")
                and ("superseded_by" not in self.columns or link.get("superseded_by") is None)
                and link["status"] not in {"failed", "error", "rejected", "rejected_done"}
                and link.get("job_status") not in {"failed", "error", "rejected", "rejected_done", "cancelled"}
                for link in self.links
            )
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        assert "RETURNING dispatch_count" in query
        assert args[0] == MILESTONE_ID
        self.claims += 1
        return {"dispatch_count": 2}


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Context:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Context()


@pytest.fixture
def run_cycle(monkeypatch):
    from app.core import db_pool
    from app.services import orchestration_limits

    async def _not_paused(*_args):
        return False, ""

    async def _open(*_args):
        return True, ""

    async def _no_repair(_conn):
        return 0

    monkeypatch.setattr(orchestration_limits, "owner_paused", _not_paused)
    monkeypatch.setattr(orchestration_limits, "goal_paused", _not_paused)
    monkeypatch.setattr(orchestration_limits, "cost_gate", _open)
    monkeypatch.setattr(orchestration_limits, "load_gate", _open)
    monkeypatch.setattr(goal_dispatch, "repair_owner_links", _no_repair)
    monkeypatch.setattr(goal_dispatch, "_ENABLED", True)
    monkeypatch.setattr(goal_dispatch, "_PROGRESS_GRACE_MIN", 45)
    sent = []
    monkeypatch.setattr(goal_dispatch, "_spawn_send", lambda **kwargs: sent.append(kwargs))

    async def _columns(conn):
        return conn.columns

    monkeypatch.setattr(goal_dispatch.goal_manager, "link_optional_columns", _columns)

    def _run(answered_at, links=None, *, later_answers=(), columns=None):
        conn = _Conn(
            [answered_at, *later_answers], links or [],
            {"link_state", "superseded_by"} if columns is None else columns,
        )
        monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
        result = asyncio.run(goal_dispatch.dispatch_pending_milestones())
        return result, conn, sent

    return _run


def test_old_answer_without_link_reaches_redispatch(run_cycle) -> None:
    answered_at = datetime.now(UTC) - timedelta(minutes=60)
    result, conn, sent = run_cycle(answered_at)
    assert result["sent"] == 1
    assert conn.claims == 1
    assert len(sent) == 1


def test_answer_with_link_is_skipped(run_cycle) -> None:
    answered_at = datetime.now(UTC) - timedelta(minutes=60)
    result, conn, sent = run_cycle(answered_at, [{"status": "running"}])
    assert result["skipped"] == 1
    assert result["sent"] == 0
    assert conn.claims == 0
    assert sent == []


def test_recent_answer_without_link_waits_for_grace(run_cycle) -> None:
    answered_at = datetime.now(UTC) - timedelta(minutes=10)
    result, conn, sent = run_cycle(answered_at)
    assert result["skipped"] == 1
    assert result["sent"] == 0
    assert conn.claims == 0
    assert sent == []


@pytest.mark.parametrize("link_state", ["orphan", "detached"])
def test_inactive_link_reaches_redispatch(run_cycle, link_state) -> None:
    answered_at = datetime.now(UTC) - timedelta(minutes=60)
    result, conn, sent = run_cycle(answered_at, [{"status": "running", "link_state": link_state}])
    assert result["sent"] == 1
    assert conn.claims == 1
    assert len(sent) == 1


@pytest.mark.parametrize("status", ["failed", "error", "rejected", "rejected_done"])
def test_failed_link_reaches_redispatch(run_cycle, status) -> None:
    answered_at = datetime.now(UTC) - timedelta(minutes=60)
    result, conn, sent = run_cycle(answered_at, [{"status": status}])
    assert result["sent"] == 1
    assert conn.claims == 1
    assert len(sent) == 1


def test_superseded_link_reaches_redispatch(run_cycle) -> None:
    answered_at = datetime.now(UTC) - timedelta(minutes=60)
    result, conn, sent = run_cycle(answered_at, [{"status": "running", "superseded_by": "new-job"}])
    assert result["sent"] == 1
    assert conn.claims == 1
    assert len(sent) == 1


def test_later_shared_session_answer_does_not_extend_grace(run_cycle) -> None:
    first_answer = datetime.now(UTC) - timedelta(minutes=60)
    unrelated_answer = datetime.now(UTC) - timedelta(minutes=5)
    result, conn, sent = run_cycle(first_answer, later_answers=[unrelated_answer])
    assert result["sent"] == 1
    assert conn.claims == 1
    assert len(sent) == 1


def test_optional_link_columns_absent_uses_legacy_filter(run_cycle) -> None:
    answered_at = datetime.now(UTC) - timedelta(minutes=60)
    result, conn, sent = run_cycle(answered_at, [{"status": "running"}], columns=set())
    assert result["skipped"] == 1
    assert conn.claims == 0
    assert sent == []


def test_live_link_without_saved_answer_is_not_redispatched(run_cycle) -> None:
    """러너가 먼저 떴고 채팅 답이 아직 저장되지 않았다 — 재발송하면 중복 작업이다."""
    result, conn, sent = run_cycle(None, [{"status": "running"}])
    assert result["skipped"] == 1
    assert result["sent"] == 0
    assert conn.claims == 0
    assert sent == []


@pytest.mark.parametrize("job_status", ["error", "cancelled", "rejected_done"])
def test_link_whose_job_already_failed_reaches_redispatch(run_cycle, job_status) -> None:
    """링크 status 는 running 으로 남았지만 실제 작업은 끝났다 — 진행으로 세지 않는다."""
    answered_at = datetime.now(UTC) - timedelta(minutes=60)
    result, conn, sent = run_cycle(answered_at, [{"status": "running", "job_status": job_status}])
    assert result["sent"] == 1
    assert conn.claims == 1
    assert len(sent) == 1


def test_shared_session_answer_before_anchor_is_ignored(run_cycle) -> None:
    # dispatched_at is approximately two hours old; the mock anchor is one
    # minute later, so this saved assistant message falls between them.
    old_answer = datetime.now(UTC) - timedelta(minutes=119, seconds=30)
    result, conn, sent = run_cycle(old_answer)
    assert result["sent"] == 1
    assert conn.claims == 1


def test_missing_anchor_falls_back_and_logs(monkeypatch, run_cycle) -> None:
    from app.core import db_pool

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)
    # The cycle fixture's connection reports no tagged dispatch message.
    conn = _Conn([datetime.now(UTC) - timedelta(minutes=60)], [], {"link_state", "superseded_by"})
    conn.anchor_missing = True
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
    _patch_dispatch_gates(monkeypatch, conn)
    asyncio.run(goal_dispatch.dispatch_pending_milestones())
    assert "goal_dispatch_anchor_missing" in log.events


def test_changed_owner_session_has_no_response(monkeypatch, run_cycle) -> None:
    from app.core import db_pool
    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)
    conn = _Conn([], [], {"link_state", "superseded_by"})
    original_fetch = conn.fetch
    async def _fetch(*args):
        rows = await original_fetch(*args)
        rows[0]["dispatched_session_id"] = "old-session"
        return rows
    conn.fetch = _fetch
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
    _patch_dispatch_gates(monkeypatch, conn)
    asyncio.run(goal_dispatch.dispatch_pending_milestones())
    assert not any("FROM chat_messages" in q for q in conn.link_queries)
    assert "goal_dispatch_anchor_missing" in log.events
