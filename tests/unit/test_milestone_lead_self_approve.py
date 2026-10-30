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
    ('done', 'a'*40, 'APPROVE', '잔존 오류', {'summary': '완료'}, False),
    ('done', 'a'*40, 'APPROVE', None, {}, False),
])
async def test_evidence(status, sha, verdict, error, ev, allowed):
    conn = AsyncMock()
    conn.fetchval.return_value = ev
    conn.fetch.side_effect = [[{'task_id': 'runner-1234abcd'}], [dict(
        job_id='runner-1234abcd', status=status, commit_hash=sha,
        review_verdict=verdict, error_detail=error,
    )], []]
    result = await review.lead_self_approval_evidence(conn, 'mid')
    assert result['ok'] is allowed
    assert bool(result['reason']) is not allowed
    assert conn.fetch.await_args_list[1].args[1] == ['runner-1234abcd']


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, review._REVIEW_MAX_ASK, 41, 45])
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
        expected_escalated = 1
    else:
        expected_escalated = 0
    assert await review.ask_pending_reviews() == {'asked': 1, 'escalated': expected_escalated}
    handoff.assert_awaited_once()
    assert handoff.await_args.args[0] == 'lead'
    assert '주도 자기 승인 가능(증거: runner-1234abcd)' in handoff.await_args.args[1]
    if count >= review._REVIEW_MAX_ASK:
        notify.assert_awaited_once()
    else:
        notify.assert_not_awaited()
    sql, _, retry = conn.fetch.await_args.args
    assert 'm.review_asked_at < NOW()' in sql
    assert retry == str(review._REVIEW_RETRY_MIN)


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


@pytest.mark.asyncio
@pytest.mark.parametrize('successor_verdict', ['APPROVE', 'REJECT', None])
@pytest.mark.parametrize('original_status', ['error', 'done'])
async def test_retry_resolves_error_only_with_later_done_approve(successor_verdict, original_status):
    resolved = successor_verdict == 'APPROVE'
    conn = AsyncMock()
    conn.fetchval.return_value = {'refs': ['runner-1234abcd', 'runner-87654321']}
    conn.fetch.side_effect = [[], [
        dict(job_id='runner-1234abcd', status=original_status, commit_hash=None,
             review_verdict=None, error_detail='approval_commit_failed'),
        dict(job_id='runner-87654321', status='done', commit_hash='a'*40,
             review_verdict='APPROVE', error_detail=None),
    ], [{'job_id': 'runner-1234abcd'}] if resolved else []]
    result = await review.lead_self_approval_evidence(conn, 'mid')
    assert result['ok'] is resolved
    assert result['resolved_errors'] == (['runner-1234abcd'] if resolved else [])
    assert ('미해결 error' in result['reason']) is not resolved
    sql, errors, marker_pattern = conn.fetch.await_args.args
    assert errors == ['runner-1234abcd']
    assert "successor.status = 'done'" in sql
    assert "successor.review_verdict = 'APPROVE'" in sql
    assert 'successor.created_at > original.created_at' in sql
    assert 'WITH candidates AS MATERIALIZED' in sql
    assert "instruction ~" in sql
    assert "strpos" not in sql
    assert marker_pattern == review._RETRY_MARKER_PATTERN
    assert "replace($2::text, '{job_id}', original.job_id)" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize('instruction,verdict,resolved', [
    ('설명\nRETRY_OF_LINK: runner-1234abcd\n완료', 'APPROVE', True),
    ('본문에서 runner-1234abcd 를 설명한다', 'APPROVE', False),
    ('RETRY_OF_LINK: runner-1234abcd', 'REJECT', False),
    ('설명 RETRY_OF_LINK: runner-1234abcd', 'APPROVE', False),
    ('RETRY_OF_LINK: 다른 작업\nrunner-1234abcd', 'APPROVE', False),
    ('RETRY_OF_LINK: runner-1234abcd0', 'APPROVE', False),
    *[(f'{marker}: runner-1234abcd', 'APPROVE', True)
      for marker in ('RETRY_OF', 'REWORK_OF', 'SUPERSEDES')],
])
async def test_retry_marker_line(instruction, verdict, resolved):
    import re

    conn = AsyncMock()
    conn.fetchval.return_value = {'refs': ['runner-1234abcd', 'runner-87654321']}

    async def fetch(sql, ids, marker_pattern=None):
        if 'goal_task_links' in sql:
            return []
        if 'WITH candidates AS MATERIALIZED' not in sql:
            return [
                dict(job_id='runner-1234abcd', status='error', commit_hash=None,
                     review_verdict=None, error_detail='failed'),
                dict(job_id='runner-87654321', status='done', commit_hash='a'*40,
                     review_verdict='APPROVE', error_detail=None),
            ]
        assert marker_pattern == review._RETRY_MARKER_PATTERN
        pattern = review._RETRY_MARKER_PATTERN.replace('{job_id}', ids[0])
        pattern = pattern.removeprefix('(?n)').replace(r'\m', r'\b').replace(r'\M', r'\b')
        return ([{'job_id': ids[0]}]
                if verdict == 'APPROVE' and re.search(pattern, instruction, re.MULTILINE)
                else [])

    conn.fetch.side_effect = fetch
    result = await review.lead_self_approval_evidence(conn, 'mid')
    assert result['ok'] is resolved
    assert result['resolved_errors'] == (['runner-1234abcd'] if resolved else [])
    assert ('미해결 error' in result['reason']) is not resolved


@pytest.mark.asyncio
async def test_invalid_retry_id_stays_unresolved(monkeypatch):
    from unittest.mock import Mock

    log = Mock()
    monkeypatch.setattr(review.logger, "info", log)
    conn = AsyncMock()
    conn.fetchval.return_value = {'summary': '완료'}
    conn.fetch.side_effect = [
        [{'task_id': 'runner-invalid.*'}, {'task_id': 'runner-87654321'}],
        [dict(job_id='runner-invalid.*', status='error', commit_hash=None,
              review_verdict=None, error_detail='failed'),
         dict(job_id='runner-87654321', status='done', commit_hash='a'*40,
              review_verdict='APPROVE', error_detail=None)],
    ]
    result = await review.lead_self_approval_evidence(conn, 'mid')
    assert result['ok'] is False
    assert result['resolved_errors'] == []
    assert conn.fetch.await_count == 2
    log.assert_called_once_with(
        "milestone_retry_invalid_job_id", job_id="runner-invalid.*", milestone_id="mid",
    )
