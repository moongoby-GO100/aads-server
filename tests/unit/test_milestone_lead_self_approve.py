"""주도 자기 승인 권한과 객관 증거 회귀 검증."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.core import db_pool
from app.services import milestone_review as review


@pytest.mark.asyncio
@pytest.mark.parametrize('lead,enabled,ok,negative,evidence_ok,expected', [
    ('session', '1', True, False, True, 'completed'),
    ('session', '1', True, False, False, 'blocked'),
    ('other', '1', True, False, True, 'blocked'),
    ('session', '0', True, False, True, 'blocked'),
    ('session', '1', False, False, False, 'in_progress'),
    ('session', '1', True, True, True, 'blocked'),
])
async def test_confirm(monkeypatch, lead, enabled, ok, negative, evidence_ok, expected):
    from app.services import tool_executor
    conn = AsyncMock()
    class Pool:
        fetchrow = AsyncMock(return_value={'tenant_id': 'tenant', 'same_tenant': True})
        fetchval = AsyncMock(side_effect=[1, lead])
        @asynccontextmanager
        async def acquire(self):
            yield conn
    monkeypatch.setattr(db_pool, 'get_pool', Pool)
    monkeypatch.setenv('MILESTONE_LEAD_SELF_APPROVE', enabled)
    monkeypatch.setattr(tool_executor, '_resolve_bound_chat_session_id', lambda _: 'session')
    evidence = AsyncMock(return_value={'ok': evidence_ok, 'jobs': ['runner-1234abcd'], 'reason': 'commit_hash 부족'})
    monkeypatch.setattr(review, 'lead_self_approval_evidence', evidence)
    confirm = AsyncMock(return_value={'status': expected})
    monkeypatch.setattr(review, 'confirm', confirm)
    result = await tool_executor.ToolExecutor._confirm_milestone(None, {
        'milestone_id': 'mid', 'ok': ok, 'negative': negative, 'reason': '검증 완료',
    })
    if expected == 'blocked':
        assert result['error'] == 'cannot_confirm_own_milestone'
        assert result['reason']
        if not evidence_ok:
            assert 'commit_hash' in result['reason']
        confirm.assert_not_awaited()
    else:
        assert result['status'] == expected
        assert confirm.await_args.kwargs['confirmer'] == 'lead_self'
        if ok:
            assert confirm.await_args.kwargs['reason'].startswith('[주도 자기 승인] jobs=runner-1234abcd')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,sha,verdict,error,ev,allowed', [
    ('done', 'a'*40, 'APPROVE', None, {'ref': 'runner-1234abcd'}, True),
    ('running', 'a'*40, 'APPROVE', None, {'ref': 'runner-1234abcd'}, False),
    ('awaiting_approval', 'a'*40, 'APPROVE', None, {'summary': '완료'}, False),
    ('error', 'a'*40, 'APPROVE', None, {'summary': '완료'}, False),
    ('done', '', 'APPROVE', None, {'summary': '완료'}, False),
    ('done', 'a'*40, 'REJECT', None, {'summary': '완료'}, False),
    ('done', 'a'*40, 'APPROVE', '미해결', {'summary': '완료'}, False),
    ('done', 'a'*40, 'APPROVE', None, {}, False),
])
async def test_evidence(status, sha, verdict, error, ev, allowed):
    conn = AsyncMock()
    conn.fetchval.return_value = ev
    conn.fetch.side_effect = [[{'task_id': 'runner-1234abcd'}], [dict(
        job_id='runner-1234abcd', status=status, commit_hash=sha,
        review_verdict=verdict, error_detail=error,
    )]]
    result = await review.lead_self_approval_evidence(conn, 'mid')
    assert result['ok'] is allowed
    assert bool(result['reason']) is not allowed
    assert conn.fetch.await_args.args[1] == ['runner-1234abcd']


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, review._REVIEW_MAX_ASK])
async def test_ask_lead(monkeypatch, count):
    from app.services import chat_service, orchestration_limits, ohvis_alert
    conn = AsyncMock()
    conn.fetch.return_value = [dict(milestone_id='mid', title='단계', goal_id='goal',
        goal_title='목표', project='AADS', owner='CTO', owner_session='lead',
        review_ask_count=count, evidence={'summary': '완료'}, completion_criteria='완료')]
    conn.fetchval.return_value = 'CTO'
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn
    monkeypatch.setattr(db_pool, 'get_pool', Pool)
    monkeypatch.setenv('MILESTONE_LEAD_SELF_APPROVE', '1')
    monkeypatch.setattr(orchestration_limits, 'goal_paused', AsyncMock(return_value=(False, '')))
    monkeypatch.setattr(review, '_lead_session', AsyncMock(return_value='lead'))
    monkeypatch.setattr(review, 'lead_self_approval_evidence', AsyncMock(return_value={
        'ok': True, 'jobs': ['runner-1234abcd'], 'reason': '',
    }))
    notify = AsyncMock()
    monkeypatch.setattr(ohvis_alert, 'notify', notify)
    handoff = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_service, 'handoff_internal_turn_if_standby', handoff)
    if count >= review._REVIEW_MAX_ASK:
        assert await review.ask_pending_reviews() == {'asked': 0, 'escalated': 1}
        notify.assert_awaited_once()
        handoff.assert_not_awaited()
        return
    assert await review.ask_pending_reviews() == {'asked': 1, 'escalated': 0}
    assert handoff.await_args.args[0] == 'lead'
    assert '주도 자기 승인 가능(증거: runner-1234abcd)' in handoff.await_args.args[1]
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_evidence_union_and_unresolved_jobs():
    conn = AsyncMock()
    conn.fetchval.return_value = '{"refs": ["runner-1234abcd", "runner-ffffffff"]}'
    conn.fetch.side_effect = [[{'task_id': 'runner-1234abcd'}, {'task_id': 'runner-87654321'}], [
        dict(job_id='runner-1234abcd', status='done', commit_hash='a'*40,
             review_verdict='APPROVE', error_detail=None),
        dict(job_id='runner-87654321', status='queued', commit_hash=None,
             review_verdict=None, error_detail=None),
    ]]
    result = await review.lead_self_approval_evidence(conn, 'mid')
    assert not result['ok']
    assert '활성 작업: runner-87654321' in result['reason']
    assert '연결 작업 조회 불가: runner-ffffffff' in result['reason']
    assert conn.fetch.await_args.args[1] == ['runner-1234abcd', 'runner-87654321', 'runner-ffffffff']


@pytest.mark.asyncio
async def test_evidence_no_jobs():
    conn = AsyncMock()
    conn.fetchval.return_value = {'summary': '완료 보고'}
    conn.fetch.return_value = []
    result = await review.lead_self_approval_evidence(conn, 'mid')
    assert not result['ok']
    assert '연결된 pipeline_job 없음' in result['reason']


@pytest.mark.asyncio
async def test_confirm_persists_self_approval_note(monkeypatch):
    from app.services import goal_work_item_sync, goal_manager
    conn = AsyncMock()
    conn.fetchrow.return_value = dict(status='review', title='단계', goal_id='goal',
        dispatched_at=None, dispatch_count=0)

    @asynccontextmanager
    async def transaction():
        yield
    conn.transaction = transaction

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn
    monkeypatch.setattr(db_pool, 'get_pool', Pool)
    monkeypatch.setattr(goal_work_item_sync, 'sync_milestone', AsyncMock())
    monkeypatch.setattr(goal_manager.goal_state_machine, '_update_goal_progress', AsyncMock())
    note = '[주도 자기 승인] jobs=runner-1234abcd\n검증 완료'
    result = await review.confirm('mid', True, reason=note, confirmer='lead_self', tenant_id='tenant')
    assert result['status'] == 'completed'
    sql, mid, status, review_note, tenant = conn.execute.await_args.args
    assert 'review_note = NULLIF($3' in sql
    assert (mid, status, review_note, tenant) == ('mid', 'completed', note, 'tenant')
