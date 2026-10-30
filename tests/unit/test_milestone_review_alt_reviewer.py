"""대체 확인 요청과 테넌트 경계 회귀 검증."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.core import db_pool
from app.services import milestone_review as review, orchestration_limits, ohvis_alert


@pytest.mark.asyncio
@pytest.mark.parametrize('self_owned,alternate,count,asked,escalated', [
    (True, 'qa', 0, 1, 0), (True, None, 0, 0, 1),
    (True, 'qa', review._REVIEW_MAX_ASK, 0, 1), (False, None, 0, 1, 0),
])
async def test_review_routing(monkeypatch, self_owned, alternate, count, asked, escalated):
    from app.services import chat_service
    row = dict(milestone_id='12345678-id', title='단계', goal_id='goal',
               goal_title='목표', project='AADS', owner='CTO' if self_owned else 'dev',
               owner_session='lead' if self_owned else 'dev', review_ask_count=count,
               evidence={'summary': '검증 근거'}, completion_criteria='완료')
    conn = AsyncMock()
    conn.fetch.return_value = [row]
    conn.fetchval.return_value = 'CTO'

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    monkeypatch.setattr(db_pool, 'get_pool', lambda: Pool())
    monkeypatch.setattr(orchestration_limits, 'goal_paused', AsyncMock(return_value=(False, '')))
    monkeypatch.setattr(review, '_lead_session', AsyncMock(return_value='lead'))
    alt = AsyncMock(return_value=alternate)
    monkeypatch.setattr(review, '_alt_reviewer', alt)
    notify = AsyncMock()
    monkeypatch.setattr(ohvis_alert, 'notify', notify)
    handoff = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_service, 'handoff_internal_turn_if_standby', handoff)
    assert await review.ask_pending_reviews() == dict(asked=asked, escalated=escalated)
    assert notify.await_count == escalated
    if asked:
        target, content = handoff.await_args.args
        assert target == (alternate if self_owned else 'lead')
        if self_owned:
            assert '[완료 확인 요청 — 대체 확인자]' in content
            assert '주도가 직접 맡은 단계라 주도 대신 확인을 요청합니다' in content
            assert 'MILESTONE_ID: 12345678-id' in content
    if count >= review._REVIEW_MAX_ASK:
        alt.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_scope_and_order(monkeypatch):
    conn = AsyncMock()
    conn.fetch.return_value = [{'session_id': 'lead'}, {'session_id': 'qa'}]
    monkeypatch.setattr(review, '_lead_session', AsyncMock(return_value='lead'))
    assert await review._alt_reviewer(conn, 'goal', 'owner', 'CTO') == 'qa'
    sql, *args = conn.fetch.await_args.args
    assert args == ['goal', 'owner', 'CTO']
    for guard in ('s.tenant_id = g.tenant_id', 'w.tenant_id = g.tenant_id',
                  'upper(w.project_key) = upper(g.project)',
                  "COALESCE(s.role_key, '') <> $3", 's.id IS DISTINCT FROM $2::uuid',
                  "l.task_type = 'chat_session'", 'm.owner_session_id = s.id',
                  "upper(s.role_key) ~ '(PM|QA)'", 's.updated_at DESC NULLS LAST, s.id'):
        assert guard in sql
    conn.fetch.return_value = []
    assert await review._alt_reviewer(conn, 'goal', 'owner', 'CTO') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('same_tenant,own', [(False, False), (True, True), (True, False)])
async def test_confirm_tenant_and_self_guard(monkeypatch, same_tenant, own):
    from app.services import tool_executor
    pool = AsyncMock()
    pool.fetchrow.return_value = {'tenant_id': 'tenant', 'same_tenant': same_tenant}
    pool.fetchval.return_value = own
    monkeypatch.setattr(db_pool, 'get_pool', lambda: pool)
    monkeypatch.setattr(tool_executor, '_resolve_bound_chat_session_id', lambda _: 'session')
    confirm = AsyncMock(return_value={'status': 'completed'})
    monkeypatch.setattr(review, 'confirm', confirm)
    result = await tool_executor.ToolExecutor._confirm_milestone(None, {'milestone_id': 'mid', 'ok': True})
    if not same_tenant:
        assert result['error'] == 'cannot_confirm_foreign_tenant'
        confirm.assert_not_awaited()
    elif own:
        assert result['error'] == 'cannot_confirm_own_milestone'
        confirm.assert_not_awaited()
    else:
        assert confirm.await_args.kwargs['tenant_id'] == 'tenant'


@pytest.mark.asyncio
@pytest.mark.parametrize('candidate_tenant,candidate_project,role,expected', [
    ('tenant', 'AADS', 'QA', 'candidate'),
    ('foreign', 'AADS', 'QA', None),
    ('tenant', 'OTHER', 'QA', None),
    ('tenant', 'AADS', 'CTO', None),
])
async def test_candidate_filters_execute(candidate_tenant, candidate_project, role, expected, monkeypatch):
    """Execute scope predicates against isolated relational fixtures, never production DB."""
    import re
    import sqlite3

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.create_function('regexp', 2, lambda pattern, value: bool(re.search(pattern, value or '')))
    db.executescript('''
        CREATE TABLE goals (id, tenant_id, project, owner_session_id);
        CREATE TABLE chat_sessions (id, tenant_id, workspace_id, role_key, updated_at);
        CREATE TABLE chat_workspaces (id, tenant_id, project_key);
        CREATE TABLE goal_task_links (goal_id, task_type, task_id);
        CREATE TABLE milestones (goal_id, owner_session_id);
        INSERT INTO goals VALUES ('goal', 'tenant', 'AADS', 'lead');
    ''')
    db.execute('INSERT INTO chat_workspaces VALUES (?, ?, ?)', ('w', candidate_tenant, candidate_project))
    db.execute('INSERT INTO chat_sessions VALUES (?, ?, ?, ?, ?)',
               ('candidate', candidate_tenant, 'w', role, '2026-10-30'))
    db.execute("INSERT INTO goal_task_links VALUES ('goal', 'chat_session', 'candidate')")

    class Connection:
        async def fetch(self, sql, *args):
            # Only adapt PostgreSQL cast/operator spelling; execute original predicates/order.
            sql = sql.replace('::uuid', '').replace('::text', '')
            sql = sql.replace("upper(s.role_key) ~ '(PM|QA)'", "regexp('(PM|QA)', upper(s.role_key))")
            return db.execute(sql, {str(i): value for i, value in enumerate(args, 1)}).fetchall()

    monkeypatch.setattr(review, '_lead_session', AsyncMock(return_value='lead'))
    try:
        assert await review._alt_reviewer(Connection(), 'goal', 'owner', 'CTO') == expected
        if expected:
            db.execute("DELETE FROM goal_task_links")
            db.execute("INSERT INTO milestones VALUES ('goal', 'candidate')")
            assert await review._alt_reviewer(Connection(), 'goal', 'owner', 'CTO') == expected
            db.execute("INSERT INTO chat_sessions VALUES ('recent-dev', 'tenant', 'w', 'dev', '2026-10-31')")
            db.execute("INSERT INTO milestones VALUES ('goal', 'recent-dev')")
            assert await review._alt_reviewer(Connection(), 'goal', 'owner', 'CTO') == 'candidate'
            db.execute("INSERT INTO chat_sessions VALUES ('recent-qa', 'tenant', 'w', 'PM', '2026-10-31')")
            db.execute("INSERT INTO milestones VALUES ('goal', 'recent-qa')")
            assert await review._alt_reviewer(Connection(), 'goal', 'owner', 'CTO') == 'recent-qa'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_confirm_requires_session(monkeypatch):
    from app.services import tool_executor
    monkeypatch.setattr(tool_executor, '_resolve_bound_chat_session_id', lambda _: '')
    confirm = AsyncMock()
    monkeypatch.setattr(review, 'confirm', confirm)
    pool = AsyncMock()
    monkeypatch.setattr(db_pool, 'get_pool', lambda: pool)
    result = await tool_executor.ToolExecutor._confirm_milestone(None, {'milestone_id': 'mid'})
    assert result == {
        'error': 'confirming_session_required',
        'message': '판정 세션을 확인할 수 없어 판정할 수 없다',
    }
    pool.fetchrow.assert_not_awaited()
    confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_lead_avoids_duplicate_lookup(monkeypatch):
    conn = AsyncMock()
    conn.fetch.return_value = [{'session_id': 'qa'}]
    lookup = AsyncMock()
    monkeypatch.setattr(review, '_lead_session', lookup)
    assert await review._alt_reviewer(
        conn, 'goal', 'owner', 'CTO', lead_session='lead',
    ) == 'qa'
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_scope_is_not_tenant_mismatch(monkeypatch):
    from app.services import tool_executor
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    monkeypatch.setattr(db_pool, 'get_pool', lambda: pool)
    monkeypatch.setattr(tool_executor, '_resolve_bound_chat_session_id', lambda _: 'session')
    confirm = AsyncMock()
    monkeypatch.setattr(review, 'confirm', confirm)
    result = await tool_executor.ToolExecutor._confirm_milestone(None, {'milestone_id': 'mid'})
    assert result['error'] == 'confirmation_scope_unavailable'
    confirm.assert_not_awaited()
