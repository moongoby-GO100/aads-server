import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from inspect import getsource
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import goal_report


@pytest.mark.asyncio
async def test_report_owner_priority_and_tenant_fence():
    conn = SimpleNamespace(fetchval=AsyncMock(return_value='owner'), execute=AsyncMock())
    assert await goal_report._post_to_lead(conn, 'goal', 'report')
    sql = conn.fetchval.call_args.args[0]
    assert 's.tenant_id = g.tenant_id' in sql
    assert 'ORDER BY (s.id = g.owner_session_id) DESC' in sql
    assert "s.role_key LIKE '%Lead'" in sql
    assert conn.execute.call_args.args[1] == 'owner'


@pytest.mark.asyncio
async def test_invalid_or_foreign_owner_without_valid_fallback_does_not_send():
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=None), execute=AsyncMock())
    assert not await goal_report._post_to_lead(conn, 'goal', 'report')
    conn.execute.assert_not_called()


def row(sid, milestone='m'):
    return dict(session_id=sid, goal_id='goal', milestone_id=milestone, title='막힌 항목',
                updated_at=datetime.now(timezone.utc), failed_links=2)


def setup(monkeypatch, rows, used=0):
    conn = SimpleNamespace(fetchval=AsyncMock(side_effect=[True, used]),
                           fetch=AsyncMock(return_value=rows), execute=AsyncMock(),
                           transaction=lambda: nullcontext())
    pool = SimpleNamespace(acquire=lambda: nullcontext(conn))
    monkeypatch.setattr('app.core.db_pool.get_pool', lambda: pool)
    enqueue = AsyncMock(return_value={'queue_id': 'q', 'created': True})
    monkeypatch.setitem(sys.modules, 'app.services.chat_service',
                        SimpleNamespace(enqueue_next_step_reaction=enqueue))
    monkeypatch.setenv('GOAL_BLOCKED_TRIAGE_ENABLED', 'true')
    monkeypatch.delenv('GOAL_BLOCKED_TRIAGE_MAX_SESSIONS_PER_DAY', raising=False)
    return conn, enqueue


@pytest.mark.asyncio
async def test_one_digest_per_session_and_real_queue(monkeypatch):
    conn, enqueue = setup(monkeypatch, [row('s', 'm1'), row('s', 'm2')])
    assert await goal_report.triage_blocked_milestones() == {'sent': 1}
    enqueue.assert_awaited_once()
    args, kwargs = enqueue.call_args
    assert args[0] == 's'
    assert 'm1' in args[1] and 'm2' in args[1] and 'superseded_by' in args[1]
    assert kwargs['dedupe_key'].startswith('goal_blocked_triage:s:')
    assert 'INSERT INTO chat_messages' not in getsource(goal_report.triage_blocked_milestones)
    assert any('INSERT INTO goal_report_log' in c.args[0] for c in conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_default_daily_cap_ten(monkeypatch):
    _, enqueue = setup(monkeypatch, [row(str(i)) for i in range(15)])
    assert await goal_report.triage_blocked_milestones() == {'sent': 10}
    assert enqueue.await_count == 10


@pytest.mark.asyncio
async def test_cap_counts_previous_cycles(monkeypatch):
    _, enqueue = setup(monkeypatch, [row('s')], used=10)
    assert await goal_report.triage_blocked_milestones() == {'sent': 0}
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_same_day_queue_reuse_not_counted(monkeypatch):
    _, enqueue = setup(monkeypatch, [row('s')])
    enqueue.return_value = {'queue_id': 'existing', 'created': False}
    assert await goal_report.triage_blocked_milestones() == {'sent': 0}


@pytest.mark.asyncio
async def test_disabled(monkeypatch):
    _, enqueue = setup(monkeypatch, [row('s')])
    monkeypatch.setenv('GOAL_BLOCKED_TRIAGE_ENABLED', 'false')
    assert await goal_report.triage_blocked_milestones() == {'sent': 0}
    enqueue.assert_not_called()


def test_candidate_pause_recent_links_and_24h_fences():
    sql = goal_report.BLOCKED_TRIAGE_CANDIDATES_SQL
    for part in ('g.paused_at IS NULL', 'FROM owner_pause', "INTERVAL '24 hours'",
                 'l.superseded_by IS NULL', 'q.session_id=s.id', 'q.dedupe_key =',
                 's.tenant_id=g.tenant_id', "m.status='blocked'", 'ORDER BY priority_rank'):
        assert part in sql


@pytest.mark.asyncio
async def test_remaining_daily_budget_and_override(monkeypatch):
    _, enqueue = setup(monkeypatch, [row(str(i)) for i in range(4)], used=2)
    monkeypatch.setenv('GOAL_BLOCKED_TRIAGE_MAX_SESSIONS_PER_DAY', '3')
    assert await goal_report.triage_blocked_milestones() == {'sent': 1}
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_parallel_cycle_cannot_send(monkeypatch):
    conn, enqueue = setup(monkeypatch, [row('s')])
    conn.fetchval.side_effect = [False]
    assert await goal_report.triage_blocked_milestones() == {'sent': 0}
    enqueue.assert_not_called()


def test_stage_timeout_and_common_trigger_guard():
    from pathlib import Path
    main = Path('app/main.py').read_text()
    assert 'asyncio.wait_for(triage_blocked_milestones(), _GOAL_STAGE_TIMEOUT)' in main
    chat = Path('app/services/chat_service.py').read_text()
    start = chat.index('async def enqueue_next_step_reaction(')
    assert '_safe_reaction_message(session_id, system_message)' in chat[start:start+1100]
    retry = Path('app/services/goal_retry_exhaust_triage.py').read_text()
    assert 'return await enqueue_next_step_reaction(' in retry
