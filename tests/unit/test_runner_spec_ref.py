"""SPEC_REF 파싱·보강 및 경고 모드 회귀 검증."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services import spec_ref as spec


def test_parse():
    assert spec.parse_spec_refs('SPEC_REF: first@r2\nSPEC_REF: second\nSPEC_REF: bad@rX\nSPEC_REFF: typo') == [
        {'document_key': 'first', 'revision': 2}, {'document_key': 'second', 'revision': None}]


def test_extract_limits_and_section_boundary():
    assert spec.extract_acceptance('# 문서\n## 완료 기준\n통과\n## 기타\n제외') == '## 완료 기준\n통과'
    assert len(spec.extract_acceptance('## Acceptance\n' + 'x' * 2000)) == 1500
    refs = [dict(document_key=str(i), revision=1, approved=True, content='## 성공 기준\n' + 'x'*2000) for i in range(5)]
    block = spec.build_spec_block(refs)
    assert block.count('x') <= 4000


class Pool:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []
        self.conn = self

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetch(self, sql, *args):
        assert 'COALESCE(h.approved_revision_id, h.latest_revision_id)' in sql
        assert 'project_document_goal_links' in sql
        return self.rows

    async def fetchrow(self, sql, *args):
        return self.rows[0] if self.rows else None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def bind(pool, **kwargs):
    return asyncio.run(spec.bind_runner_specs(instruction=kwargs.pop('instruction', '작업'),
        goal_id=kwargs.pop('goal_id', 'goal'), tenant_id='tenant', project='AADS',
        job_id='runner-test', pool=pool, **kwargs))


@pytest.mark.parametrize('approved', [True, False])
def test_goal_auto_persists_pin_and_audit(approved):
    pool = Pool([dict(document_key='plan', revision=3, approved=approved, title='계획', content='## 완료 기준\n통과')])
    warning = bind(pool)
    update, event = pool.calls
    assert 'UPDATE pipeline_jobs' in update[0]
    assert 'SPEC_REF: plan@r3' in update[1][2]
    assert event[1][0] == 'runner_spec_ref' and event[1][2] == 'runner-test'
    detail = json.loads(event[1][4])
    assert detail == dict(goal_id='goal', refs=[dict(document_key='plan', revision=3, approved=approved)], source='goal_auto')
    assert (warning is None) == approved
    assert event[1][3] == ('spec_bound' if approved else 'spec_unapproved')


def test_block_idempotent():
    pool = Pool([dict(document_key='plan', revision=1, approved=True)])
    bind(pool, instruction=spec.MARKER + '\nSPEC_REF: plan@r1')
    assert len(pool.calls) == 1 and 'canonical_gate_events' in pool.calls[0][0]


@pytest.mark.parametrize('instruction', ['SPEC_REF: missing@r1', 'SPEC_REF: typo@rX'])
def test_explicit_invalid_without_goal(instruction):
    assert bind(Pool(), instruction=instruction, goal_id=None)['verdict'] == 'spec_ref_invalid'


def test_missing_and_no_goal():
    assert bind(Pool())['verdict'] == 'spec_missing'
    pool = Pool()
    assert bind(pool, goal_id=None) is None and not pool.calls


def test_off_no_io(monkeypatch):
    monkeypatch.setenv('CANONICAL_GATE_MODE', 'off')
    pool = Pool()
    pool.acquire = lambda: pytest.fail('off acquired pool')
    assert bind(pool) is None


def test_db_error_fail_open():
    pool = Pool()
    pool.fetch = AsyncMock(side_effect=RuntimeError('db down'))
    assert bind(pool) is None


def test_timeout_fail_open(monkeypatch):
    monkeypatch.setenv('CANONICAL_GATE_TIMEOUT_MS', '20')
    pool = Pool()
    async def slow(*args):
        await asyncio.sleep(1)
    pool.fetch = slow
    assert bind(pool) is None


def test_both_submission_entrypoints_call_binder():
    import ast
    source = Path('app/api/pipeline_runner.py').read_text()
    tree = ast.parse(source)
    for name in ('submit_job', 'submit_batch'):
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'bind_runner_specs'
                   for n in ast.walk(node))


from tests.unit import test_canonical_gate_entrypoints as _gate_entry  # noqa: E402

runner_env = _gate_entry.runner_env


@pytest.mark.parametrize('entrypoint', ['single', 'batch'])
def test_real_submission_binds_goal_specs(runner_env, monkeypatch, entrypoint):
    from tests.unit.test_canonical_gate_entrypoints import _submit, _batch
    pool = Pool([dict(document_key='spec', revision=7, approved=True, title='명세', content='## 완료 기준\n완료')])
    original = spec.bind_runner_specs
    async def bind_here(**kwargs):
        return await original(**kwargs, pool=pool)
    monkeypatch.setattr(spec, 'bind_runner_specs', bind_here)
    if entrypoint == 'single':
        _submit(runner_env)
        count = 1
    else:
        _batch(runner_env)
        count = 3
    updates = [args for sql, args in pool.calls if 'UPDATE pipeline_jobs' in sql]
    events = [args for sql, args in pool.calls if 'canonical_gate_events' in sql]
    assert len(updates) == len(events) == count
    assert all('SPEC_REF: spec@r7' in args[2] for args in updates)
    assert all(args[0] == 'runner_spec_ref' for args in events)


def test_explicit_omitted_revision_resolves_and_records():
    pool = Pool([dict(document_key='spec', revision=4, approved=True, title='명세', content='')])
    assert bind(pool, instruction='SPEC_REF: spec', goal_id=None) is None
    assert 'SPEC_REF: spec@r4' in pool.calls[0][1][2]
    assert json.loads(pool.calls[1][1][4])['source'] == 'explicit'


def test_audit_failure_keeps_submission_and_warning():
    pool = Pool()
    pool.execute = AsyncMock(side_effect=RuntimeError('check constraint'))
    assert bind(pool)['verdict'] == 'spec_missing'


def test_huge_revision_is_invalid_without_raising():
    assert bind(Pool(), instruction='SPEC_REF: spec@r' + '9'*5000)['verdict'] == 'spec_ref_invalid'
