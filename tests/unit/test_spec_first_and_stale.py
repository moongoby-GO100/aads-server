"""문서 우선 안내와 승인 경고는 제출·승인을 차단하지 않는다."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services import spec_ref as spec
from tests.unit import test_canonical_gate_entrypoints as entry
from tests.unit import test_runner_approval_precheck as approval

runner_env = entry.runner_env
approve_env = approval.approve_env


class Pool:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [dict(document_key='plan', revision=3)]
        self.execute = AsyncMock()

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetch(self, sql, *args):
        assert 'h.latest_revision_id' in sql
        assert 'COALESCE(h.approved_revision_id' not in sql
        return self.rows

    async def fetchrow(self, sql, *args):
        assert args[-1] is None
        return self.rows[0] if self.rows else None


def check(pool=None, **kw):
    return asyncio.run(spec.check_spec_warning(instruction=kw.pop('instruction', '범위 변경'),
        goal_id=kw.pop('goal_id', 'goal'), tenant_id='tenant', project='AADS',
        job_id='runner-test', pool=pool or Pool(), **kw))


@pytest.mark.parametrize('signal', ['범위 변경', '요구 변경', '사양 변경', '기획 변경', 'SPEC_CHANGE: 추가'])
def test_change_warns(signal):
    pool = Pool()
    warning = check(pool, instruction=signal + '\nSPEC_REF: plan@r2')
    assert warning['verdict'] == 'spec_first'
    assert 'canonical_document_register 로 plan 새 판본' in warning['message']
    assert pool.execute.call_args.args[1] == 'chat_spec_first'


@pytest.mark.parametrize('instruction', ['범위 변경\nSPEC_REF: plan@r3', '버그 수정'])
def test_no_false_positive(instruction):
    assert check(instruction=instruction) is None


def test_no_document_or_goal():
    assert check(Pool([])) is None
    assert check(goal_id=None) is None


@pytest.mark.parametrize('revision,expected', [(2, True), (3, False), (4, False)])
def test_approval_revision(revision, expected):
    pool = Pool()
    warning = check(pool, instruction=f'SPEC_REF: plan@r{revision}', approval=True)
    assert bool(warning) == expected
    if expected:
        assert warning['message'] == '⚠️ 낡은 문서 기준: 작업은 plan r2, 현재 r3'
        assert pool.execute.call_args.args[1] == 'runner_approve_stale'


def test_db_and_audit_fail_open():
    pool = Pool()
    pool.fetch = AsyncMock(side_effect=RuntimeError('db'))
    assert check(pool) is None
    pool = Pool()
    pool.execute.side_effect = RuntimeError('audit')
    assert check(pool)['verdict'] == 'spec_first'
    pool.fetchrow = AsyncMock(side_effect=RuntimeError('db'))
    assert check(pool, instruction='SPEC_REF: plan@r2', approval=True) is None


@pytest.mark.parametrize('batch', [False, True])
def test_submission_continues_with_warning(runner_env, monkeypatch, batch):
    original = spec.check_spec_warning
    async def warning(**kwargs):
        return await original(**{**kwargs, 'instruction': '범위 변경', 'goal_id': 'goal', 'pool': Pool()})
    monkeypatch.setattr(spec, 'check_spec_warning', warning)
    result = entry._batch(runner_env) if batch else entry._submit(runner_env)
    jobs = result['jobs'] if batch else [result.model_dump()]
    assert all(j['job_id'].startswith('runner-') for j in jobs)
    if not batch:
        assert result.status == 'queued'
    assert all('spec_first' in str(j['canonical_gate']) for j in jobs)


def test_approval_continues_with_warning(approve_env, monkeypatch):
    job = approval.job(instruction='SPEC_REF: plan@r2')
    approve_env(approval.ApproveConn(job))
    original = spec.check_spec_warning
    async def warning(**kwargs):
        return await original(**{**kwargs, 'pool': Pool()})
    monkeypatch.setattr(spec, 'check_spec_warning', warning)
    result = approval.approve()
    assert result['action'] == 'approve'
    assert '낡은 문서 기준' in result['message']


def test_approval_db_failure_does_not_block(approve_env, monkeypatch):
    conn = approval.ApproveConn(approval.job(instruction='SPEC_REF: plan@r2'))
    approve_env(conn)
    original = spec.check_spec_warning
    pool = Pool()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError('db'))
    async def warning(**kwargs):
        return await original(**{**kwargs, 'pool': pool})
    monkeypatch.setattr(spec, 'check_spec_warning', warning)
    assert approval.approve()['action'] == 'approve'
    assert any('UPDATE pipeline_jobs' in sql for sql in conn.executed)
