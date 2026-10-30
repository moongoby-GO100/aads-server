"""문서 대조는 판정을 바꾸지 않고 승인 없는 초안만 등록한다."""
import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.services import code_reviewer as cr
from app.services import spec_drift as drift


def details(**kw):
    return dict(verdict='APPROVE', correctness=.9, security=.9, scope_compliance=.9,
                preservation=.9, quality=.9, issues=[], summary='검수 완료', **kw)


@pytest.mark.parametrize('extra', [{}, {'spec_alignment': []},
    {'spec_alignment': 'aligned', 'spec_delta': 3},
    {'spec_alignment': 'deviates', 'spec_delta': 'x' * 601}])
def test_defaults(extra):
    result = cr._validate_review_details(details(**extra))
    assert result['spec_alignment'] == 'not_applicable'
    assert result['spec_delta'] == ''


@pytest.mark.parametrize('alignment', ['aligned', 'deviates', 'spec_update_needed', 'not_applicable'])
def test_valid_fields(alignment):
    result = cr._validate_review_details(details(spec_alignment=alignment, spec_delta='차이'))
    assert result['spec_alignment'] == alignment


class Connection:
    def __init__(self):
        self.created = None
        self.head = dict(id='head', kind='spec', generation=2, latest_revision_id='latest')

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, sql, *args):
        return 4 if 'SELECT revision' in sql else 'tenant'

    async def fetchrow(self, sql, *args):
        if 'FOR UPDATE' in sql:
            return self.head
        if 'idempotency_key' in sql:
            return {'revision': 4} if self.created else None
        return dict(content='# 기준\n', version='1.0.0', title='기준 명세')


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.parametrize('alignment', ['deviates', 'spec_update_needed'])
def test_draft_once_no_approval(alignment):
    async def run():
        conn = Connection()
        async def create(c, tenant, actor, project, body):
            assert c is conn
            assert body.expected_generation == 2
            assert body.idempotency_key == 'spec-drift:runner-test:sample'
            assert body.source_task_id == 'runner-test'
            assert body.content.startswith('# 기준\n')
            assert '## 구현 중 변경 (runner-test,' in body.content
            conn.created = body
            return {}
        creator = AsyncMock(side_effect=create)
        with patch('app.core.db_pool.get_pool', return_value=Pool(conn)), patch(
            'app.api.canonical_documents.create_revision_in_tx', creator):
            for delta in ['변경 사항', '재검수 다른 설명']:
                result = await drift.register_spec_drift('SPEC_REF: sample@r2', 'runner-test', 'AADS',
                    {'spec_alignment': alignment, 'spec_delta': delta})
                assert result == [{'document_key': 'sample', 'revision': 4}]
        assert creator.await_count == 1
        # Fake connection exposes no execute/approval operation; only canonical creation can write.
        assert not hasattr(conn, 'execute')
    asyncio.run(run())


def test_fail_open():
    async def run():
        with patch('app.core.db_pool.get_pool', side_effect=RuntimeError('unavailable')):
            result = await drift.register_spec_drift('SPEC_REF: sample', 'runner-test', 'AADS',
                {'spec_alignment': 'spec_update_needed', 'spec_delta': '차이'})
            assert result == []
    asyncio.run(run())


@pytest.mark.asyncio
async def test_prompt_and_decision_unchanged_without_ref():
    async def run():
        assert await drift.review_spec_prompt('일반 지시', 'runner-test', 'AADS') == ''
        prompts = []
        async def inline(func, *args, **kwargs):
            return func(*args, **kwargs)
        for alignment in ['aligned', 'deviates', 'spec_update_needed']:
            model = AsyncMock(return_value=json.dumps(details(spec_alignment=alignment, spec_delta='차이')))
            with patch.object(cr.asyncio, 'to_thread', inline), patch.object(cr, '_job_review_context', AsyncMock(return_value={})), patch.object(
                cr, '_get_review_models', AsyncMock(return_value=['codex:gpt-test'])), patch.object(
                cr, '_call_review_model', model), patch.object(cr, '_save_review_result', AsyncMock()), patch(
                'app.services.review_advisory.schedule_review_advisory'):
                verdict = await cr.review_code_diff(project='AADS', job_id='runner-test', instruction='일반 지시',
                    diff='diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n a = 1\n+b = 2\n', files_changed=['a.py'])
            assert verdict.verdict == 'APPROVE'
            assert verdict.score == pytest.approx(.9)
            assert model.await_count == 1
            prompts.append(model.call_args.kwargs['prompt'])
        assert prompts[0] == prompts[1] == prompts[2]
        assert '[기준 문서 대조' not in prompts[0]
    await run()


def test_prompt_reads_pinned_acceptance():
    async def run():
        conn = Connection()
        conn.fetchrow = AsyncMock(return_value=dict(document_key='sample', revision=2,
            content='# 명세\n## 완료 기준\n- 동작 확인\n## 기타\n제외'))
        with patch('app.core.db_pool.get_pool', return_value=Pool(conn)):
            prompt = await drift.review_spec_prompt('SPEC_REF: sample@r2', 'runner-test', 'AADS')
        assert '동작 확인' in prompt and '제외' not in prompt
        assert conn.fetchrow.call_args.args[-1] == 2
        assert 'spec_alignment' in prompt
    asyncio.run(run())


def test_creation_failure_is_open_and_never_claims_registered():
    async def run():
        conn = Connection()
        value = {'spec_alignment': 'spec_update_needed', 'spec_delta': '동작 변경', 'summary': '검수 완료'}
        with patch('app.core.db_pool.get_pool', return_value=Pool(conn)), patch(
            'app.api.canonical_documents.create_revision_in_tx', AsyncMock(side_effect=RuntimeError('write failed'))):
            await drift.apply_spec_review('SPEC_REF: sample', 'runner-test', 'AADS', value)
        assert value['spec_drafts'] == []
        assert '초안 등록' not in value['summary']
    asyncio.run(run())


def test_registered_notification():
    async def run():
        value = {'spec_alignment': 'deviates', 'spec_delta': '변경', 'summary': '검수 완료'}
        with patch.object(drift, 'register_spec_drift', AsyncMock(return_value=[{'document_key': 'sample', 'revision': 4}])):
            await drift.apply_spec_review('SPEC_REF: sample', 'runner-test', 'AADS', value)
        assert '⚠️ 문서 수정 필요 — sample r4 초안 등록(승인 대기)' in value['summary']
    asyncio.run(run())


def test_apply_spec_review_handles_null_summary():
    async def run():
        value = {'spec_alignment': 'aligned', 'summary': None}
        await drift.apply_spec_review('SPEC_REF: sample', 'runner-test', 'AADS', value)
        assert '문서 대조:' in value['summary']
    asyncio.run(run())


def test_review_code_diff_survives_spec_review_exception():
    async def run():
        async def inline(func, *args, **kwargs):
            return func(*args, **kwargs)

        model = AsyncMock(return_value=json.dumps(details()))
        with patch.object(cr.asyncio, 'to_thread', inline), patch.object(
            cr, '_job_review_context', AsyncMock(return_value={})), patch.object(
            cr, '_get_review_models', AsyncMock(return_value=['codex:gpt-test'])), patch.object(
            cr, '_call_review_model', model), patch.object(cr, '_save_review_result', AsyncMock()), patch(
            'app.services.spec_drift.parse_spec_refs', side_effect=RuntimeError('parse failed')), patch(
            'app.services.review_advisory.schedule_review_advisory'):
            verdict = await cr.review_code_diff(project='AADS', job_id='runner-test',
                instruction='SPEC_REF: sample', diff='diff --git a/a.py b/a.py\n', files_changed=['a.py'])
        assert verdict.verdict == 'APPROVE'
    asyncio.run(run())
