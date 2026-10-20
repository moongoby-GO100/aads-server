from __future__ import annotations

from datetime import datetime
from inspect import getsource
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services import goal_report


def test_weekly_event_key_uses_kst_iso_week() -> None:
    # Sunday UTC is already Monday in Korea, so the KST week must win.
    observed = datetime(2026, 9, 20, 15, 30, tzinfo=ZoneInfo("UTC"))
    assert goal_report._weekly_event_key(observed) == "weekly_progress:2026-W39"


def test_weekly_report_is_session_only_and_idempotent() -> None:
    source = getsource(goal_report.report_weekly_goal_progress)

    assert "INSERT INTO chat_messages" in source
    assert "goal_weekly_report" in source
    assert "ON CONFLICT (goal_id, subject_id, event) DO NOTHING" in source
    assert "_telegram(" not in source
    assert "notify(" not in source
    # structlog reserves the positional event name. Passing ``event=`` again
    # records the report and then crashes with "multiple values for event".
    assert "report_event=event" in source
    assert ", event=event" not in source


def test_scheduler_registers_monday_kst_goal_report() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert 'id="weekly_goal_progress"' in source
    assert 'CronTrigger(day_of_week="mon", hour=0, minute=0, timezone="UTC")' in source

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _milestone(**values):
    return {"id": "m1", "title": "작업", "status": "in_progress",
            "owner_session_id": "owner", "owner_role_key": "Developer",
            "failed_task_id": None, **values}


def test_weekly_real_block_signals_and_terminal_exclusion():
    assert goal_report._weekly_blockers(_milestone(dispatch_blocked_at="now"))
    assert goal_report._weekly_blockers(_milestone(owner_session_id=None)) == ["담당 세션 미지정"]
    assert goal_report._weekly_blockers(_milestone(dispatch_note="과거 메모")) == []
    assert goal_report._weekly_blockers(_milestone(retry_accounting={"exhausted": True}))
    for status in ("completed", "cancelled"):
        assert goal_report._weekly_blockers(_milestone(
            status=status, dispatch_blocked_at="now", owner_session_id=None)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result, expected", [
    ({"created": True, "card_id": "approval"}, "복구 요청 접수"),
    ({"candidate_alive": True}, "복구 요청 접수"),
    ({"why": "retry_exhausted"}, "복구 보류/차단"),
    ({"why": "proposal_failed"}, "복구 보류/차단"),
])
async def test_followup_reuses_formal_recovery(monkeypatch, result, expected):
    from app.services import goal_failure_retry
    proposer = AsyncMock(return_value=result)
    monkeypatch.setattr(goal_failure_retry, "ensure_retry_candidate", proposer)
    monkeypatch.setitem(sys.modules, "app.main", SimpleNamespace(
        _is_active_api_container_for_background_jobs=lambda: True))
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=True))
    text = await goal_report._weekly_followup(conn, {"goal_id": "g1"}, _milestone(failed_task_id="runner-12345678"))
    assert expected in text
    assert "실행 성공" not in text
    if result.get("created"):
        assert "실행 시작 미확인" in text
    proposer.assert_awaited_once_with(conn, milestone_id="m1", goal_id="g1",
                                    failed_task_id="runner-12345678", reason="주간 목표 보고: 실패 링크 복구")
    assert "pg_advisory_unlock" in conn.fetchval.call_args.args[0]


@pytest.mark.asyncio
async def test_followup_no_unauthorized_execution(monkeypatch):
    from app.services import goal_failure_retry
    proposer = AsyncMock()
    monkeypatch.setattr(goal_failure_retry, "ensure_retry_candidate", proposer)
    for milestone in (_milestone(owner_session_id=None), _milestone(owner_role_key="CEO"),
                      _milestone(failed_task_id=None), _milestone(status="completed")):
        await goal_report._weekly_followup(None, {"goal_id": "g1"}, milestone)
    proposer.assert_not_awaited()


@pytest.mark.asyncio
async def test_followup_inactive_and_failed_connection(monkeypatch):
    from app.services import goal_failure_retry
    proposer = AsyncMock(side_effect=RuntimeError("proposal unavailable"))
    monkeypatch.setattr(goal_failure_retry, "ensure_retry_candidate", proposer)
    main = SimpleNamespace(_is_active_api_container_for_background_jobs=lambda: False)
    monkeypatch.setitem(sys.modules, "app.main", main)
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=True))
    assert "활성 슬롯 처리 대기" in await goal_report._weekly_followup(conn, {"goal_id": "g1"}, _milestone(failed_task_id="runner-12345678"))
    proposer.assert_not_awaited()
    main._is_active_api_container_for_background_jobs = lambda: True
    assert "복구 연결 실패" in await goal_report._weekly_followup(conn, {"goal_id": "g1"}, _milestone(failed_task_id="runner-12345678"))
    assert "pg_advisory_unlock" in conn.fetchval.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("signals", [
    {"dispatch_blocked_at": "now"}, {"owner_session_id": None},
    {"failed_task_id": "runner-12345678"},
    {"retry_accounting": {"exhausted": True}},
    {"dispatch_blocked_at": "now", "failed_task_id": "runner-12345678"},
])
async def test_weekly_concurrent_report_goal_blocked_and_retry_after_delivery(monkeypatch, signals):
    from contextlib import asynccontextmanager

    from app.core import db_pool
    from app.services import goal_failure_retry, goal_manager

    claims = set()
    messages = []
    actions = AsyncMock(side_effect=["복구 연결 실패 — 재시도 대기", "복구 요청 접수", "복구 요청 접수"])
    monkeypatch.setattr(goal_report, "_weekly_followup", actions)
    monkeypatch.setattr(goal_manager, "link_optional_columns", AsyncMock(return_value=set()))

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, query, *args):
            if "INSERT INTO chat_messages" in query:
                messages.append(args)

        async def fetchval(self, query, *args):
            key = tuple(args)
            if key in claims:
                return None
            claims.add(key)
            return 1

        async def fetch(self, query, *args):
            if "FROM goals g" in query:
                return [{"goal_id": "g1", "title": "목표", "project": "AADS",
                         "goal_status": "blocked", "tenant_id": "t1", "target_session_id": "owner",
                         "milestone_total": 1, "milestone_completed": 0, "milestone_blocked": 1}]
            if "FROM goal_retry_executions" in query:
                return []
            from app.services.goal_binding import DONE_JOB_STATUSES, FAILED_JOB_STATUSES
            assert args[1] == sorted(FAILED_JOB_STATUSES)
            assert args[2] == sorted(DONE_JOB_STATUSES)
            assert "j.phase" in query
            assert "OR (upper(trim(COALESCE(j.project" in query
            assert args[3] == ["AADS"]
            # Execute the actual SQL predicate with SQLite array expansion and
            # compare against the shared project-aware state contract.
            import json
            import sqlite3
            from app.services.goal_binding import normalize_job_state_for_project
            predicate = query.split("AND (lower(trim(COALESCE(j.status", 1)[1]
            predicate = "(lower(trim(COALESCE(j.status" + predicate.split("AND l.link_state", 1)[0]
            # Optional link predicates may be absent in this harness.
            predicate = predicate.split("ORDER BY l.created_at", 1)[0].strip()
            for index, values in ((2, args[1]), (3, args[2]), (4, args[3])):
                array = "(SELECT value FROM json_each('" + json.dumps(values) + "'))"
                predicate = predicate.replace(f"<> ALL(${index}::text[])", "NOT IN " + array)
                predicate = predicate.replace(f"= ANY(${index}::text[])", "IN " + array)
            with sqlite3.connect(":memory:") as db:
                for project in ("AADS", "OTHER"):
                    for status in ("done", "failed", "running", "completed"):
                        for phase in ("review_failed", "done", ""):
                            matched = db.execute(
                                "SELECT " + predicate + " FROM (SELECT ? AS status, ? AS phase, ? AS project) j",
                                (status, phase, project),
                            ).fetchone()[0]
                            assert bool(matched) == (normalize_job_state_for_project(status, phase, project) == "failed")

            return [_milestone(**signals)]

    @asynccontextmanager
    async def acquire():
        yield Connection()

    monkeypatch.setattr(db_pool, "get_pool", lambda: SimpleNamespace(acquire=acquire))
    await asyncio.gather(goal_report.report_weekly_goal_progress(), goal_report.report_weekly_goal_progress())
    await goal_report.report_weekly_goal_progress()
    assert len(messages) == 1
    assert "목표 자체 blocked" in messages[0][1]
    assert "차단 1건" in messages[0][1]
    for reason in goal_report._weekly_blockers(_milestone(**signals)):
        assert reason in messages[0][1]
    assert "복구 연결 실패" in messages[0][1]
    assert "실행 성공을 뜻하지 않습니다" in messages[0][1]
    # Report dedupe must not suppress retries of a failed formal connection.
    assert actions.await_count == 3


@pytest.mark.asyncio
async def test_missing_owner_uses_approval_only_with_existing_lead(monkeypatch):
    from app.services import owner_session_provision
    request = AsyncMock(return_value={"request_id": "card-1"})
    monkeypatch.setattr(owner_session_provision, "request_owner_session", request)
    monkeypatch.setitem(sys.modules, "app.main", SimpleNamespace(
        _is_active_api_container_for_background_jobs=lambda: True))
    conn = SimpleNamespace(fetchval=AsyncMock(side_effect=[True, "lead", True]))
    text = await goal_report._weekly_followup(
        conn, {"goal_id": "g1", "lead_session_id": "lead", "tenant_id": "t1", "project": "AADS"},
        _milestone(owner_session_id=None))
    assert "담당 생성 승인 대기" in text
    assert "자동 생성/실행 없음" in text
    request.assert_awaited_once_with(goal_id="g1", role_key="Developer", project="AADS", requester_session_id="lead")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_owner", [False, True])
async def test_paused_goal_never_requests_recovery_or_owner(monkeypatch, missing_owner):
    from app.services import goal_failure_retry, owner_session_provision
    recover = AsyncMock()
    request = AsyncMock()
    monkeypatch.setattr(goal_failure_retry, "ensure_retry_candidate", recover)
    monkeypatch.setattr(owner_session_provision, "request_owner_session", request)
    text = await goal_report._weekly_followup(
        None, {"goal_id": "g1", "paused_at": "now", "lead_session_id": "lead"},
        _milestone(owner_session_id=None if missing_owner else "owner",
                   failed_task_id="runner-12345678"))
    assert "일시중지" in text
    recover.assert_not_awaited()
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lock", "unlock"])
async def test_followup_lock_errors_do_not_abort_reports(monkeypatch, failure):
    from app.services import goal_failure_retry
    proposer = AsyncMock(return_value={"created": True})
    monkeypatch.setattr(goal_failure_retry, "ensure_retry_candidate", proposer)
    monkeypatch.setitem(sys.modules, "app.main", SimpleNamespace(
        _is_active_api_container_for_background_jobs=lambda: True))
    conn = SimpleNamespace(fetchval=AsyncMock(side_effect=(
        [RuntimeError("lock unavailable")] if failure == "lock"
        else [True, RuntimeError("unlock unavailable")]
    )))
    text = await goal_report._weekly_followup(
        conn, {"goal_id": "g1"}, _milestone(failed_task_id="runner-12345678"))
    if failure == "lock":
        assert "복구 연결 실패" in text
        proposer.assert_not_awaited()
        assert conn.fetchval.await_count == 1
    else:
        assert "복구 요청 접수" in text
        assert "실행 시작 미확인" in text
        proposer.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize('why, expected', [('successor_active', '기존 후속 작업 진행 중'),
                                         ('successor_completed', '기존 후속 작업 완료')])
async def test_successor_is_not_reported_blocked(monkeypatch, why, expected):
    from app.services import goal_failure_retry
    monkeypatch.setitem(sys.modules, 'app.main', SimpleNamespace(
        _is_active_api_container_for_background_jobs=lambda: True))
    monkeypatch.setattr(goal_failure_retry, 'ensure_retry_candidate', AsyncMock(
        return_value={'why': why, 'successor': 'runner-successor'}))
    text = await goal_report._weekly_followup(SimpleNamespace(fetchval=AsyncMock(return_value=True)),
        {'goal_id': 'g1'}, _milestone(failed_task_id='runner-failed'))
    assert expected in text
    assert '차단' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('signal', [{'dispatch_blocked_at': 'now'}, {'status': 'blocked'},
                                    {'retry_accounting': {'exhausted': True}}])
async def test_existing_owner_formal_question_weekly_atomic_claim(monkeypatch, signal):
    import sqlite3
    from app.services import session_relay
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE claims (goal TEXT, subject TEXT, event TEXT, UNIQUE(goal,subject,event))')
    class Connection:
        async def fetchval(self, query, *args):
            if 'pg_' in query or 'SELECT count' in query:
                return True
            # Exercise the actual claim uniqueness contract, rather than a
            # canned mock call count. Concurrent coroutines share this store.
            return db.execute('INSERT INTO claims VALUES (?,?,?) ON CONFLICT DO NOTHING RETURNING rowid', args).fetchone()
    gate = asyncio.Event()
    async def ask(**kwargs):
        assert kwargs['origin_session_id'] == 'lead'
        assert kwargs['target'] == 'owner'
        assert kwargs['question'].startswith(f"GOAL_ID: {goal['goal_id']}\n")
        assert '실행 승인이 아닙니다' in kwargs['question']
        gate.set()
        await asyncio.sleep(0)
        return {'sent': True, 'queued': True, 'relay_id': 'relay-1'}
    relay = AsyncMock(side_effect=ask)
    monkeypatch.setattr(session_relay, 'ask', relay)
    monkeypatch.setitem(sys.modules, 'app.main', SimpleNamespace(
        _is_active_api_container_for_background_jobs=lambda: True))
    goal = {'goal_id': '123e4567-e89b-12d3-a456-426614174000',
            'lead_session_id': 'lead', 'tenant_id': 't1'}
    results = await asyncio.gather(*(goal_report._weekly_followup(Connection(), goal, _milestone(**signal)) for _ in range(3)))
    assert gate.is_set()
    assert all('실행 시작 미확인' in text for text in results)
    assert db.execute('SELECT count(*) FROM claims').fetchone()[0] == 1
    relay.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_owner_question_failed_delivery_is_visible_and_retryable(monkeypatch):
    from app.services import session_relay
    relay = AsyncMock(return_value={'sent': False, 'error': 'target_not_found'})
    monkeypatch.setattr(session_relay, 'ask', relay)
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=True), execute=AsyncMock())
    text = await goal_report._weekly_owner_question(conn,
        {'goal_id': 'g1', 'lead_session_id': 'lead', 'tenant_id': 't1'}, _milestone())
    assert '전달 실패' in text and 'target_not_found' in text
    assert 'DELETE FROM goal_report_log' in conn.execute.call_args.args[0]


@pytest.mark.asyncio
async def test_owner_question_busy_pair_releases_only_item_claim_and_retries(monkeypatch):
    import sqlite3
    from app.services import session_relay

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE claims (goal TEXT, subject TEXT, event TEXT, UNIQUE(goal,subject,event))')

    class Connection:
        async def fetchval(self, query, *args):
            if 'SELECT count(*)' in query:
                return True
            return db.execute(
                'INSERT INTO claims VALUES (?,?,?) ON CONFLICT DO NOTHING RETURNING rowid', args
            ).fetchone()

        async def execute(self, query, *args):
            assert 'DELETE FROM goal_report_log' in query
            db.execute('DELETE FROM claims WHERE goal=? AND subject=? AND event=?', args)

    relay = AsyncMock(side_effect=[
        {'sent': False, 'error': 'already_in_flight', 'active_relay_id': 'other-relay'},
        {'sent': True, 'queued': True, 'relay_id': 'relay-2'},
    ])
    monkeypatch.setattr(session_relay, 'ask', relay)
    conn = Connection()
    goal_id = '123e4567-e89b-12d3-a456-426614174000'
    goal = {'goal_id': goal_id, 'lead_session_id': 'lead', 'tenant_id': 't1'}
    milestone = _milestone(id='m2')

    first = await goal_report._weekly_owner_question(conn, goal, milestone)
    assert '진행 중' in first and '보류' in first
    assert db.execute('SELECT count(*) FROM claims').fetchone()[0] == 0
    second = await goal_report._weekly_owner_question(conn, goal, milestone)
    assert '접수' in second
    assert db.execute('SELECT count(*) FROM claims').fetchone()[0] == 1
    assert all(call.kwargs['question'].startswith(f'GOAL_ID: {goal_id}\n') for call in relay.await_args_list)
    db.close()


@pytest.mark.asyncio
async def test_owner_question_ambiguous_error_keeps_claim(monkeypatch):
    from app.services import session_relay
    monkeypatch.setattr(session_relay, 'ask', AsyncMock(side_effect=TimeoutError('unknown acceptance')))
    monkeypatch.setitem(sys.modules, 'app.main', SimpleNamespace(
        _is_active_api_container_for_background_jobs=lambda: True))
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=True), execute=AsyncMock())
    text = await goal_report._weekly_followup(conn,
        {'goal_id': 'g1', 'lead_session_id': 'lead', 'tenant_id': 't1'}, _milestone(dispatch_blocked_at='now'))
    assert '연결 실패' in text
    conn.execute.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['completed', 'cancelled', 'archived'])
async def test_terminal_goal_never_contacts_owner(status):
    text = await goal_report._weekly_followup(None,
        {'goal_id': 'g1', 'goal_status': status}, _milestone(dispatch_blocked_at='now'))
    assert '후속 요청 없음' in text


@pytest.mark.asyncio
async def test_goal_only_blocked_same_lead_cannot_create_relay():
    conn = SimpleNamespace(fetchval=AsyncMock())
    text = await goal_report._weekly_owner_question(conn,
        {'goal_id': 'g1', 'lead_session_id': 'owner'},
        {'id': 'g1', 'owner_session_id': 'owner'})
    assert '별도 기존 주도 세션 필요' in text
    conn.fetchval.assert_not_awaited()
