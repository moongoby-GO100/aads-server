"""Cross-company selection, enforcement, and legacy settings contracts."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.services import doc_cross_review as cross
from app.services import code_reviewer as code

CLAUDE = 'claude-sonnet-5-5'
OPUS = 'claude-opus-5-5'
CODEX = 'codex:gpt-6.1-sol'
ASTRA = 'codex:gpt-6-astra'


@pytest.mark.parametrize('author', [CLAUDE, CODEX, 'unknown'])
@pytest.mark.parametrize('models', [[], [CLAUDE, OPUS], [CODEX, ASTRA], [CLAUDE, OPUS, CODEX, ASTRA]])
@pytest.mark.parametrize('unavailable', [None, CLAUDE, CODEX])
def test_selection_matrix(author, models, unavailable):
    available = set(models) - {unavailable}
    picks, state = cross.pick_reviewers('D3', author, models, available=available)
    assert all(p.model in available and p.model != author for p in picks)
    if state == 'ok':
        assert len(picks) == 2
        assert {cross.provider_of(p.model) for p in picks} == {'anthropic', 'openai'}
    else:
        assert state in ('blocked_no_cross', 'blocked_no_second')


def test_higher_list_and_failure_exclusion():
    picks, status = cross.pick_reviewers('D2', CLAUDE, [CLAUDE], available={CLAUDE, CODEX, ASTRA}, extra_lists=[[CODEX, ASTRA]], exclude={CODEX})
    assert status == 'ok' and picks[0].model == ASTRA


def review(model, verdict='pass'):
    return {'reviewer_model': model, 'reviewer_provider': cross.provider_of(model), 'verdict': verdict,
            'cross_company': cross.provider_of(model) == 'openai', 'created_at': 'now'}


def test_d3_requires_two_companies_and_distinct_models():
    assert cross.summarize('D3', CLAUDE, [review(CODEX)])['review_state'] != 'passed'
    assert cross.summarize('D3', CLAUDE, [review(CODEX), review(ASTRA)])['review_state'] != 'passed'
    assert cross.summarize('D3', CLAUDE, [review(CODEX), review(OPUS)])['review_state'] == 'passed'
    assert cross.summarize('D3', CLAUDE, [review(CODEX), review(OPUS), review(OPUS, 'reject')])['review_state'] == 'rejected'


def test_gate_409_and_auto_schedule(monkeypatch):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 'rev', 'doc_tier': 'D2', 'author_model': CLAUDE}
    conn.fetch.return_value = []
    schedule = __import__('unittest.mock', fromlist=['Mock']).Mock()
    monkeypatch.setattr(cross, 'schedule', schedule)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cross.enforce(conn, 'tenant', 'AADS', {'id': 'head'}, 'rev'))
    assert exc.value.status_code == 409 and exc.value.detail['code'] == 'cross_review_required'
    schedule.assert_called_once()


def test_gate_rollback_switch_and_d1_approve(monkeypatch):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 'rev', 'doc_tier': 'D1'}
    conn.fetch.return_value = []
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cross.enforce(conn, 't', 'AADS', {'id': 'h'}, 'rev', approve=True))
    assert exc.value.detail['code'] == 'approval_not_required'
    monkeypatch.setenv('DOC_CROSS_REVIEW_ENFORCE', '0')
    assert asyncio.run(cross.enforce(conn, 't', 'AADS', {}, None)) == {}


def test_legacy_six_sizes_and_new_validation():
    from app.api.pipeline_runner import _RunnerModelConfigUpdate, _RunnerModelItem
    legacy = ['XS', 'S', 'M', 'L', 'XL', 'AI_REVIEW']
    assert len(_RunnerModelConfigUpdate(configs=[{'size': s, 'models': [CLAUDE]} for s in legacy]).configs) == 6
    for size in ['AI_REVIEW_LIGHT', 'AI_REVIEW_STD', 'AI_REVIEW_HEAVY']:
        with pytest.raises(ValidationError, match='두 회사'):
            _RunnerModelItem(size=size, models=[CLAUDE])
        assert _RunnerModelItem(size=size, models=[CLAUDE, CODEX]).models == [CLAUDE, CODEX]


def test_code_priority_and_nonblocking_fallback():
    assert code.cross_first([CLAUDE, OPUS, CODEX], CLAUDE) == [CODEX, CLAUDE, OPUS]
    assert code.cross_first([CLAUDE, OPUS], CLAUDE) == [CLAUDE, OPUS]
    assert code.cross_first([CLAUDE, CODEX], CODEX) == [CLAUDE, CODEX]
    assert [code.review_config_for_size(s) for s in ['XS', 'S', 'M', 'L', 'XL']] == [
        'AI_REVIEW_LIGHT', 'AI_REVIEW_LIGHT', 'AI_REVIEW_STD', 'AI_REVIEW_HEAVY', 'AI_REVIEW_HEAVY']


class ReviewDB:
    def __init__(self, models, *, budget=0):
        self.models = models
        self.rows = []
        self.budget = budget

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetch(self, sql, *args):
        return self.rows

    async def fetchval(self, sql, *args):
        if 'document_cross_review_leases' in sql:
            return True
        if 'count(*)' in sql:
            return self.budget
        if 'runner_model_config' in sql:
            return self.models
        return None

    async def execute(self, sql, *args):
        if sql.startswith('INSERT INTO project_document_reviews'):
            self.rows.append({'reviewer_model': args[4], 'reviewer_provider': args[5], 'role': args[6],
                              'cross_company': args[7], 'verdict': args[8], 'reason': args[9], 'created_at': 'now'})


def run_review(monkeypatch, db, call, tier='D2'):
    monkeypatch.setattr(cross, 'get_pool', lambda: db)
    monkeypatch.setattr(code, '_call_review_model', call)
    monkeypatch.setattr(cross, 'notify_blocked', AsyncMock())
    return asyncio.run(cross.execute_review('tenant', 'AADS', {'id': 'head'}, {
        'id': 'revision', 'doc_tier': tier, 'author_model': CLAUDE, 'title': '검토 문서', 'content': '# 검토 문서'}))


def test_cli_failure_is_recorded_and_excluded(monkeypatch):
    db = ReviewDB([CLAUDE, CODEX, ASTRA])
    call = AsyncMock(side_effect=[RuntimeError('unavailable'), '{"verdict":"pass","reason":"정합성 확인"}'])
    result = run_review(monkeypatch, db, call)
    assert result['review_state'] == 'passed'
    assert [(r['reviewer_model'], r['verdict']) for r in db.rows] == [(CODEX, 'attempt_failed'), (ASTRA, 'pass')]
    assert [c.kwargs['model'] for c in call.await_args_list] == [CODEX, ASTRA]


def test_no_cross_blocks_and_notifies_without_cli(monkeypatch):
    db = ReviewDB([CLAUDE, OPUS])
    call = AsyncMock()
    assert run_review(monkeypatch, db, call)['review_state'] == 'blocked'
    call.assert_not_awaited()
    cross.notify_blocked.assert_awaited_once()
    assert db.rows[-1]['verdict'] == 'blocked' and db.rows[-1]['reason'] == 'blocked_no_cross'


def test_d3_executes_both_companies(monkeypatch):
    db = ReviewDB([CLAUDE, OPUS, CODEX])
    call = AsyncMock(return_value='{"verdict":"pass","reason":"검토 통과"}')
    assert run_review(monkeypatch, db, call, 'D3')['review_state'] == 'passed'
    assert [c.kwargs['model'] for c in call.await_args_list] == [CODEX, OPUS]
    assert db.rows[-1]['role'] == 'same_company_other_model'


def test_daily_limit_blocks_with_notification(monkeypatch):
    monkeypatch.setenv('AADS_DOC_TIER_D3_DAILY_LIMIT', '2')
    db = ReviewDB([CLAUDE, OPUS, CODEX], budget=2)
    call = AsyncMock()
    assert run_review(monkeypatch, db, call, 'D3')['review_state'] == 'blocked'
    call.assert_not_awaited()
    cross.notify_blocked.assert_awaited_once()
    assert db.rows[-1]['reason'].startswith('blocked_daily_limit')


def test_d3_approval_gate_checks_records(monkeypatch):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 'r', 'doc_tier': 'D3', 'author_model': CLAUDE}
    conn.fetch.return_value = [review(CODEX)]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cross.enforce(conn, 't', 'AADS', {'id': 'h'}, 'r', approve=True))
    assert exc.value.detail['code'] == 'cross_review_required'
    conn.fetch.return_value = [review(CODEX), review(OPUS)]
    assert asyncio.run(cross.enforce(conn, 't', 'AADS', {'id': 'h'}, 'r', approve=True))['review_state'] == 'passed'


def test_owner_web_push_is_primary(monkeypatch):
    from app.services import push_notifications, ceo_notify
    db = ReviewDB([])
    db.rows = [{'user_id': 'owner'}]
    db.fetch = AsyncMock(return_value=db.rows)
    monkeypatch.setattr(cross, 'get_pool', lambda: db)
    push = AsyncMock(return_value={'sent': 1})
    monkeypatch.setattr(push_notifications, 'send_web_push_to_user', push)
    monkeypatch.setattr(ceo_notify, 'send_ceo_text', AsyncMock())
    asyncio.run(cross.notify_blocked('tenant', 'AADS', 'blocked_no_cross'))
    assert push.await_args.kwargs['payload']['url'] == '/docs'
    assert push.await_args.kwargs['user_id'] == 'owner'
    assert "role='owner'" in db.fetch.await_args.args[0] and "status='active'" in db.fetch.await_args.args[0]


def test_legacy_put_replay_preserves_all_six_rows(monkeypatch):
    from app.api import pipeline_runner
    from app.core import db_pool
    db = ReviewDB([])
    db.execute = AsyncMock()
    monkeypatch.setattr(db_pool, 'get_pool', lambda: db)
    body = pipeline_runner._RunnerModelConfigUpdate(configs=[
        {'size': size, 'models': [CLAUDE]} for size in ['XS', 'S', 'M', 'L', 'XL', 'AI_REVIEW']])
    assert asyncio.run(pipeline_runner.update_runner_model_config(body))['status'] == 'ok'
    assert [call.args[1] for call in db.execute.await_args_list] == ['XS', 'S', 'M', 'L', 'XL', 'AI_REVIEW']


def test_review_config_size_and_legacy_fallback(monkeypatch):
    from app.core import db_pool
    db = ReviewDB([])
    db.fetchrow = AsyncMock(side_effect=[None, {'models': [CLAUDE, CODEX]}])
    monkeypatch.setattr(db_pool, 'get_pool', lambda: db)
    assert asyncio.run(code._get_review_models('L')) == [CLAUDE, CODEX]
    assert db.fetchrow.await_args_list[0].args[1] == 'AI_REVIEW_HEAVY'
    assert "size = 'AI_REVIEW'" in db.fetchrow.await_args_list[1].args[0]


def test_code_feedback_records_non_cross_without_blocking(monkeypatch):
    from app.core import db_pool
    db = ReviewDB([])
    db.execute = AsyncMock()
    monkeypatch.setattr(db_pool, 'get_pool', lambda: db)
    monkeypatch.setattr(code, '_job_review_context', AsyncMock(return_value={'actual_model': CLAUDE, 'size': 'S'}))
    verdict = code.ReviewVerdict(verdict='APPROVE', score=1, feedback={}, issues=[])
    asyncio.run(code._save_review_result(job_id='job', project='AADS', verdict=verdict, diff_size=20, model_used=OPUS, cost=0))
    import json
    assert json.loads(db.execute.await_args.args[5])['cross'] is False
    assert db.execute.await_args.args[3] == 'APPROVE'


def test_d1_review_runs_without_creating_approval(monkeypatch):
    db = ReviewDB([CLAUDE, CODEX])
    call = AsyncMock(return_value='{"verdict":"pass","reason":"검토 통과"}')
    assert run_review(monkeypatch, db, call, 'D1')['review_state'] == 'passed'
    call.assert_awaited_once()


def test_historical_revision_is_not_automatically_rereviewed(monkeypatch):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 'r', 'doc_tier': None, 'content': '기존 기획 문서'}
    conn.fetch.return_value = []
    schedule = __import__('unittest.mock', fromlist=['Mock']).Mock()
    monkeypatch.setattr(cross, 'schedule', schedule)
    result = asyncio.run(cross.enforce(conn, 't', 'AADS', {'id': 'h', 'kind': 'plan'}, 'r'))
    assert result['doc_tier'] == 'D2' and result['review_state'] == 'not_required'
    schedule.assert_not_called()


def test_empty_specialized_config_falls_back_to_legacy(monkeypatch):
    from app.core import db_pool
    db = ReviewDB([])
    db.fetchrow = AsyncMock(side_effect=[{'models': []}, {'models': [CLAUDE, CODEX]}])
    monkeypatch.setattr(db_pool, 'get_pool', lambda: db)
    assert asyncio.run(code._get_review_models('S')) == [CLAUDE, CODEX]


def test_failed_attempt_preserves_pass_but_reject_replaces_it():
    assert cross.summarize('D2', CLAUDE, [review(CODEX), review(CODEX, 'attempt_failed')])['review_state'] == 'passed'
    assert cross.summarize('D2', CLAUDE, [review(CODEX), review(CODEX, 'reject'), review(CODEX, 'attempt_failed')])['review_state'] == 'rejected'


def test_historical_d1_approval_exempt():
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 'r', 'doc_tier': None, 'content': '보고서'}
    conn.fetch.return_value = []
    assert asyncio.run(cross.enforce(conn, 't', 'AADS', {'id': 'h', 'kind': 'report'}, 'r', approve=True))['review_state'] == 'not_required'


def test_disabled_gate_has_no_db_access(monkeypatch):
    monkeypatch.setenv('DOC_CROSS_REVIEW_ENFORCE', '0')
    conn = AsyncMock()
    assert asyncio.run(cross.enforce(conn, 't', 'AADS', {}, 'r', approve=True)) == {}
    conn.fetchrow.assert_not_awaited()


def test_split_never_calls_cli(monkeypatch):
    call = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        run_review(monkeypatch, ReviewDB([]), call, 'SPLIT')
    assert exc.value.detail['code'] == 'document_split_required'
    call.assert_not_awaited()


def test_lock_miss_never_calls_cli(monkeypatch):
    db = ReviewDB([CLAUDE, CODEX])
    db.fetchval = AsyncMock(return_value=None)
    call = AsyncMock()
    assert run_review(monkeypatch, db, call)['review_state'] == 'pending'
    call.assert_not_awaited()


def test_cli_runs_after_pool_release(monkeypatch):
    class Pool(ReviewDB):
        depth = 0

        async def __aenter__(self):
            self.depth += 1
            return self

        async def __aexit__(self, *args):
            self.depth -= 1

    db = Pool([CLAUDE, CODEX])

    async def call(**kwargs):
        assert db.depth == 0
        return '{"verdict":"pass","reason":"확인"}'

    assert run_review(monkeypatch, db, call)['review_state'] == 'passed'


def test_blocked_d3_does_not_reserve_budget(monkeypatch):
    db = ReviewDB([CLAUDE, OPUS])
    original = db.execute
    db.execute = AsyncMock(side_effect=original)
    assert run_review(monkeypatch, db, AsyncMock(), 'D3')['review_state'] == 'blocked'
    assert not any('INSERT INTO document_cross_review_budget' in c.args[0] for c in db.execute.await_args_list)


def test_already_review_is_idempotent_before_stale_and_gate(monkeypatch):
    from app.api import canonical_documents as docs
    conn = AsyncMock()
    conn.fetchval.return_value = 'review'
    gate = AsyncMock(side_effect=AssertionError('must not enforce'))
    monkeypatch.setattr(cross, 'enforce', gate)
    result = asyncio.run(docs.review_revision_in_tx(conn, 't', 'AADS', {
        'id': 'h', 'generation': 9, 'latest_revision_id': 'other'}, 'r', 1, 'actor'))
    assert result == {'status': 'review', 'idempotent': True}
    gate.assert_not_awaited()


@pytest.mark.parametrize('case,status', [('ok', 200), ('missing', 404), ('stale', 409), ('forbidden', 403)])
def test_cross_review_http_endpoint(monkeypatch, case, status):
    from fastapi import FastAPI
    import httpx
    from app.api import canonical_documents as docs
    import uuid
    revision_id = uuid.uuid4()
    db = ReviewDB([])
    db.fetchrow = AsyncMock(return_value={'id': revision_id, 'doc_tier': 'D2'})
    monkeypatch.setattr(docs, 'get_pool', lambda: db)
    authorize = AsyncMock(return_value=('tenant', 'actor'))
    if case == 'forbidden':
        authorize.side_effect = HTTPException(403, 'forbidden')
    monkeypatch.setattr(docs, '_authorize', authorize)
    monkeypatch.setattr(docs, '_head', AsyncMock(return_value=None if case == 'missing' else {
        'id': 'h', 'generation': 2 if case == 'stale' else 1, 'latest_revision_id': revision_id}))
    execute = AsyncMock(return_value={'doc_tier': 'D2', 'review_state': 'passed', 'review_models': []})
    monkeypatch.setattr(cross, 'execute_review', execute)
    app = FastAPI()
    app.include_router(docs.router, prefix="/api/v1")
    async def user_context():
        return {}
    app.dependency_overrides[docs.WRITE.dependency] = user_context
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.post('/api/v1/projects/AADS/documents/test/cross-review', json={
                'revision_id': str(revision_id), 'expected_generation': 1})
    response = asyncio.run(request())
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()['review_state'] == 'passed'
    else:
        execute.assert_not_awaited()


def test_list_batches_reviews_for_all_documents(monkeypatch):
    from app.api import canonical_documents as docs
    rows = [{'id': 'h'+str(i), 'document_key': 'doc-'+str(i), 'kind': 'plan',
             'latest_revision_id': 'r'+str(i), 'approved_revision_id': None,
             'doc_tier': 'D2', 'author_model': CLAUDE, 'author_provider': 'anthropic'} for i in range(3)]
    db = ReviewDB([])
    db.fetch = AsyncMock(side_effect=[rows, [dict(review(CODEX), revision_id='r1')]])
    monkeypatch.setattr(docs, 'get_pool', lambda: db)
    monkeypatch.setattr(docs, '_authorize', AsyncMock(return_value=('tenant', 'actor')))
    result = asyncio.run(docs.list_documents('AADS', q=None, kind=None, approved_only=False, limit=50, context={}))
    assert [d['review_state'] for d in result['documents']] == ['pending', 'passed', 'pending']
    assert db.fetch.await_count == 2
    assert db.fetch.await_args_list[1].args[1] == ['r0', 'r1', 'r2']


def test_check_expansion_and_rollback_preserve_operator_rows():
    from pathlib import Path
    original = Path('migrations/090_media_llm_routing_admin_hardening.sql').read_text()
    up = Path('migrations/20261029_doc_cross_review.sql').read_text()
    down = Path('migrations/rollback/20261029_doc_cross_review.down.sql').read_text()
    assert 'CONSTRAINT runner_model_config_size_chk' in original
    assert 'ADD CONSTRAINT runner_model_config_size_chk' in up
    assert down.index('INSERT INTO runner_model_config_cross_review_rollback_backup') < down.index('DELETE FROM runner_model_config WHERE')
    assert 'SELECT * FROM runner_model_config WHERE size IN' in down
    for size in ('AI_REVIEW_LIGHT', 'AI_REVIEW_STD', 'AI_REVIEW_HEAVY'):
        assert size in up and size in down


def test_split_gate_rejects_without_scheduling(monkeypatch):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 'r', 'doc_tier': 'SPLIT'}
    conn.fetch.return_value = []
    schedule = __import__('unittest.mock', fromlist=['Mock']).Mock()
    monkeypatch.setattr(cross, 'schedule', schedule)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cross.enforce(conn, 't', 'AADS', {'id': 'h'}, 'r'))
    assert exc.value.detail['code'] == 'document_split_required'
    schedule.assert_not_called()


def test_lost_lease_discards_cli_result(monkeypatch):
    db = ReviewDB([CLAUDE, CODEX])
    original = db.fetchval

    async def value(sql, *args):
        if sql.startswith('SELECT owner FROM document_cross_review_leases'):
            return None
        return await original(sql, *args)

    db.fetchval = value
    call = AsyncMock(return_value='{"verdict":"pass","reason":"통과"}')
    assert run_review(monkeypatch, db, call)['review_state'] == 'pending'
    assert not db.rows


def test_d3_failure_exhaustion_releases_daily_reservation(monkeypatch):
    db = ReviewDB([CLAUDE, OPUS, CODEX])
    original = db.execute
    db.execute = AsyncMock(side_effect=original)
    call = AsyncMock(side_effect=RuntimeError('unavailable'))
    assert run_review(monkeypatch, db, call, 'D3')['review_state'] == 'blocked'
    assert any(c.args[0].startswith('DELETE FROM document_cross_review_budget') for c in db.execute.await_args_list)
