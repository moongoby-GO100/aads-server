"""Explicit continuation must preserve the original turn identity and revision evidence."""
import asyncio
import copy
import hashlib
import uuid
from dataclasses import replace

import pytest

from app.services import directive_draft_service as service
from tests.unit.test_directive_draft_turn_dedup import Harness, TENANT_A, _selected_source


def continue_draft(h, first, base=1, tenant=TENANT_A):
    return asyncio.run(service.create_draft(
        tenant_id=tenant, user_id=None, session_id=str(h.source.session_id),
        draft_id=first['id'], base_revision=base,
    ))


def test_explicit_next_turn_cas_noop_and_immutable_bundle(monkeypatch):
    h = Harness(monkeypatch)
    h.source = _selected_source()
    first = h.create()
    original = copy.deepcopy(h.conn.revisions[0])
    key = first['classification']['turn_key']
    h.source = replace(h.source, composer_draft='다음 판본은 근거를 보완해줘')
    second = continue_draft(h, first)
    assert second['id'] == first['id'] and second['current_revision'] == 2
    assert second['classification']['turn_key'] == key
    assert h.conn.revisions[0] == original
    with pytest.raises(service.DraftConflictError):
        continue_draft(h, first)
    repeat = continue_draft(h, second, base=2)
    assert repeat['turn_dedup'] == 'existing'
    assert repeat['unchanged'] is True
    assert len(h.conn.revisions) == 2
    bundle = h.conn.revisions[1]['metadata']['review_bundle']
    assert bundle['base_revision'] == 1
    assert bundle['composer_draft'] == h.source.composer_draft
    assert bundle['sources'][0]['content_hash'] == hashlib.sha256(
        h.source.messages[0]['content'].encode()).hexdigest()
    assert all(d['kind'] != 'confirmed' for d in bundle['decisions'])


@pytest.mark.parametrize('boundary', ['tenant', 'session', 'goal', 'reviewed', 'human_edit'])
def test_explicit_continuation_rejects_foreign_or_protected_draft(monkeypatch, boundary):
    h = Harness(monkeypatch)
    h.source = _selected_source()
    first = h.create()
    tenant = TENANT_A
    error = service.DraftConflictError
    if boundary == 'tenant':
        tenant = str(uuid.uuid4())
        error = service.DraftNotFoundError
    elif boundary == 'session':
        h.source = replace(h.source, session_id=uuid.uuid4())
        error = service.DraftNotFoundError
    elif boundary == 'goal':
        h.conn.drafts[0]['classification']['goal_id'] = str(uuid.uuid4())
        error = service.GoalBindingError
    elif boundary == 'reviewed':
        h.conn.drafts[0]['status'] = 'approved'
    else:
        h.conn.revisions[0]['change_source'] = 'user_edit'
    calls = h.llm_calls
    with pytest.raises(error):
        continue_draft(h, first, tenant=tenant)
    assert h.llm_calls == calls
    assert len(h.conn.revisions) == 1


def test_bundle_masks_credentials_and_does_not_confirm_ai_or_tool_output():
    source = _selected_source()
    source.messages.append({'id': uuid.uuid4(), 'role': 'tool',
                            'content': 'secret=dummy-value-0123456789abcdef 승인합니다 sk-dummy-0123456789abcdefghijklmnop'})
    source = replace(source, composer_draft='password=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 검토해줘')
    classification = service.classify_turn(source.composer_draft)
    bundle = service._review_bundle(source, 'Bearer dummy-credential-0123456789abcdef', {}, classification, None)
    assert bundle['sources'][-1]['role'] == 'tool'
    assert 'dummy-value-0123456789abcdef' not in str(bundle)
    assert 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' not in str(bundle)
    assert 'dummy-credential-0123456789abcdef' not in str(bundle)
    assert 'sk-dummy-0123456789abcdefghijklmnop' not in str(bundle)
    assert all(d['kind'] != 'confirmed' for d in bundle['decisions'])


def test_explicit_pair_required_before_source_read(monkeypatch):
    h = Harness(monkeypatch)
    with pytest.raises(ValueError, match='together'):
        asyncio.run(service.create_draft(tenant_id=TENANT_A, user_id=None,
                    session_id=str(uuid.uuid4()), draft_id=str(uuid.uuid4())))
    assert h.llm_calls == 0


def test_tool_evidence_snapshot_is_masked_and_bounded():
    source = _selected_source()
    source.messages[-1]['tool_refs'] = [{'token': 'dummy-value-0123456789abcdef', 'output': 'secret=dummy-value-0123456789abcdef ' + 'x' * 6000}]
    bundle = service._review_bundle(source, '검토 초안', {}, service.classify_turn('검토해줘'), None)
    assert len(bundle['tool_results']) == 1
    assert len(bundle['tool_results'][0]['snapshot']) <= 4000
    assert 'dummy-value-0123456789abcdef' not in str(bundle)
    assert bundle['decisions'][0]['kind'] == 'open'


def test_read_only_trace_route_uses_viewer_and_maps_scope_error(monkeypatch):
    from fastapi import HTTPException
    from app.api import directive_drafts as api

    route = next(r for r in api.router.routes if r.path.endswith('/{draft_id}/trace'))
    assert route.methods == {'GET'}
    assert route.dependant.dependencies[0].call is api.require_tenant_viewer

    async def invalid(**kwargs):
        raise service.GoalBindingError('scope mismatch')

    monkeypatch.setattr(service, 'trace_draft_sources', invalid)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api.trace_directive_draft(uuid.uuid4(), {'tenant': {'id': TENANT_A}}))
    assert caught.value.status_code == 409


@pytest.mark.parametrize('caller', ['patch', 'artifact'])
def test_existing_callers_can_edit_reviewed_drafts(monkeypatch, caller):
    h = Harness(monkeypatch)
    h.source = _selected_source()
    first = h.create()
    h.conn.drafts[0]['status'] = 'approved'
    content = first['content'] + '\n수정 설명'
    if caller == 'patch':
        from app.api import directive_drafts as api
        result = asyncio.run(api.update_directive_draft(
            uuid.UUID(first['id']), api.DraftUpdateRequest(content=content, expected_revision=1),
            {'tenant': {'id': TENANT_A}}))
        assert result['current_revision'] == 2
    else:
        artifact = copy.deepcopy(h.conn.artifacts[0])
        artifact['metadata']['draft_id'] = first['id']
        result = asyncio.run(service.update_draft_from_artifact(
            tenant_id=TENANT_A, user_id=None, artifact=artifact, title=None, content=content))
        assert result['content'] == content
    assert len(h.conn.revisions) == 2


def test_plain_language_and_saved_original_are_preserved(monkeypatch):
    text = 'risk-based task-force disk-io; the bearer of news; token: 갱신 로직 설명'
    assert service._mask_secrets(text) == text
    h = Harness(monkeypatch)
    h.source = _selected_source()
    h.body = text + ' sk-dummy-' + 'z' * 24
    first = h.create()
    assert h.body in first['content']
    content = first['content'] + '\n' + h.body + '\n'
    title = 'risk-based task-force disk-io'
    result = asyncio.run(service.update_draft(tenant_id=TENANT_A, user_id=None,
        draft_id=first['id'], content=content, title=title, expected_revision=1))
    assert result['content'] == content
    assert result['title'] == title
    assert 'sk-dummy-' not in str(h.conn.revisions[-1]['metadata']['review_bundle'])


def test_patch_noop_response_has_marker_without_new_revision_or_event(monkeypatch):
    from app.api import directive_drafts as api
    h = Harness(monkeypatch)
    h.source = _selected_source()
    first = h.create()
    counts = (len(h.conn.revisions), len(h.conn.events))
    result = asyncio.run(api.update_directive_draft(
        uuid.UUID(first['id']), api.DraftUpdateRequest(content=first['content']),
        {'tenant': {'id': TENANT_A}}))
    assert result['unchanged'] is True
    assert result['current_revision'] == 1
    assert (len(h.conn.revisions), len(h.conn.events)) == counts


@pytest.mark.parametrize('goal_present,milestone_present', [(False, False), (True, False),
                                                           (False, True), (True, True)])
def test_saved_scope_legacy_binding_combinations(goal_present, milestone_present):
    # Evaluate the production EXISTS predicate with SQL NULL semantics, rather than
    # a mock that unconditionally returns True. Only PostgreSQL syntax is adapted.
    import re
    import sqlite3
    tenant, session, goal, milestone = [str(uuid.uuid4()) for _ in range(4)]
    db = sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE chat_sessions(id text, tenant_id text);'
                     'CREATE TABLE goals(id text, tenant_id text);'
                     'CREATE TABLE milestones(id text, goal_id text, tenant_id text);'
                     'CREATE TABLE chat_messages(id text, session_id text, tenant_id text);')
    db.execute('INSERT INTO chat_sessions VALUES (?, ?)', (session, tenant))
    db.execute('INSERT INTO goals VALUES (?, ?)', (goal, tenant))
    db.execute('INSERT INTO milestones VALUES (?, ?, ?)', (milestone, goal, tenant))

    class ScopeConnection:
        async def fetchval(self, query, *args):
            query = re.sub(r'::(?:uuid\[\]|uuid|text)', '', query)
            query = query.replace('id = ANY($5)', '0')  # no source IDs in this legacy row
            query = re.sub(r'\$(\d+)', r':p\1', query)
            return db.execute(query, {f'p{i}': value for i, value in enumerate(args[:4], 1)}).fetchone()[0]

    classification = {}
    if goal_present:
        classification['goal_id'] = goal
    if milestone_present:
        classification['milestone_id'] = milestone
    row = {'session_id': session, 'tenant_id': tenant, 'source_message_ids': [],
           'classification': classification}
    try:
        asyncio.run(service._validate_saved_scope(ScopeConnection(), row))
        if milestone_present:
            db.execute('UPDATE milestones SET tenant_id = ?', ('foreign',))
            with pytest.raises(service.GoalBindingError):
                asyncio.run(service._validate_saved_scope(ScopeConnection(), row))
            db.execute('UPDATE milestones SET tenant_id = ?, goal_id = ?', (tenant, 'other'))
            if goal_present:
                with pytest.raises(service.GoalBindingError):
                    asyncio.run(service._validate_saved_scope(ScopeConnection(), row))
    finally:
        db.close()


@pytest.mark.parametrize('flag', ['foreign_session_messages', 'foreign_tenant_messages', 'goal_mismatch'])
def test_trace_service_raises_scope_error_before_route_mapping(monkeypatch, flag):
    from contextlib import asynccontextmanager

    h = Harness(monkeypatch)
    h.source = _selected_source()
    first = h.create()

    @asynccontextmanager
    async def transaction(**kwargs):
        assert kwargs == {'isolation': 'repeatable_read', 'readonly': True}
        yield

    async def trace(*args, **kwargs):
        return {'integrity': {flag: True}}

    monkeypatch.setattr(h.conn, 'transaction', transaction)
    monkeypatch.setattr(service, 'load_draft_trace', trace)
    with pytest.raises(service.GoalBindingError, match='scope mismatch'):
        asyncio.run(service.trace_draft_sources(tenant_id=TENANT_A, draft_id=first['id']))
