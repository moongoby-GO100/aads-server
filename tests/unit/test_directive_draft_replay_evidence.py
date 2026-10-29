"""M7 replay compatibility and revision evidence regressions without a live DB."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from app.services import directive_draft_service as service
from tests.unit.test_directive_draft_turn_dedup import Harness, TENANT_A

CORPUS = json.loads((Path(__file__).parents[2] / 'tests/fixtures/planning_replay_corpus_20260923.json').read_text())


def turn(seq):
    return next(t for t in CORPUS['turns'] if t['seq'] == seq)


def source(seq=8):
    t = turn(seq)
    return service.DraftSource(
        session_id=uuid.UUID(CORPUS['source']['session_id']), workspace_id=None,
        session_title='재현', workspace_name='재현', project_key='AADS',
        messages=[{'id': uuid.UUID(t['message_id']), 'role': 'user', 'content': t['text_head']}],
    )


@pytest.mark.parametrize('seq', [8, 10])
def test_single_user_turn_key_preserves_legacy_message_identity(seq):
    s = source(seq)
    raw = f"{TENANT_A}|{s.session_id}|{turn(seq)['message_id']}"
    assert service._turn_key(TENANT_A, s) == 'ctx:' + hashlib.sha256(raw.encode()).hexdigest()[:32]
    assert service._turn_key(TENANT_A, s) == service._turn_key(TENANT_A, replace(s, project_key='SF'))


def test_multimessage_key_preserves_same_legacy_composition():
    s = replace(source(), messages=source().messages + source(10).messages)
    raw = f"{TENANT_A}|{s.session_id}|{','.join(sorted(str(m['id']) for m in s.messages))}"
    assert service._turn_key(TENANT_A, s) == 'ctx:' + hashlib.sha256(raw.encode()).hexdigest()[:32]
    assert service._turn_key(TENANT_A, s) == service._turn_key(TENANT_A, replace(s, project_key='SF'))


def test_original_duplicate_ids_reuse_revision_without_replacing_turn_key(monkeypatch):
    h = Harness(monkeypatch)
    h.source = source(8)
    first = h.create()
    h.source = source(10)
    assert turn(8)['message_id'].startswith('d3254e03')
    assert turn(10)['message_id'].startswith('c5973979')
    assert turn(8)['content_md5'] == turn(10)['content_md5']
    assert service._turn_key(TENANT_A, source(8)) != service._turn_key(TENANT_A, source(10))
    second = h.create()
    assert second['id'] == first['id'] and second['turn_dedup'] == 'existing'
    assert second['classification']['turn_key'] == first['classification']['turn_key']
    assert len(h.conn.revisions) == len(h.conn.drafts) == len(h.conn.events) == 1
    # Deliberate regeneration on the original message still creates a new revision.
    h.source = source(8)
    h.body = '의도적으로 보완한 설계'
    revised = h.create()
    assert revised['id'] == first['id'] and revised['current_revision'] == 2


@pytest.mark.parametrize('boundary', ['tenant', 'session', 'project', 'composer', 'different_input', 'changed_output'])
def test_identical_resend_does_not_cross_context_boundaries(monkeypatch, boundary):
    h = Harness(monkeypatch)
    h.source = source()
    first = h.create()
    h.source = source(10)
    tenant = TENANT_A
    if boundary == 'tenant':
        tenant = str(uuid.uuid4())
    elif boundary == 'session':
        h.source = replace(h.source, session_id=uuid.uuid4())
    elif boundary == 'project':
        h.source = replace(h.source, project_key='SF')
    elif boundary == 'composer':
        h.source = replace(h.source, composer_draft='이번에는 별도 실행')
    elif boundary == 'different_input':
        h.source.messages[0]['content'] = '다른 요청'
    else:
        h.body = '명시적으로 달라진 산출물'
    assert h.create(tenant)['id'] != first['id']


def test_create_revision_whitelists_classification_and_keeps_turn_kind(monkeypatch):
    h = Harness(monkeypatch)
    h.source = source()
    classification = {'turn_kind': 'confirmed', 'decisions': [], 'turn_kind_evidence': [],
                      'unexpected': 'must not leak', 'absent': None}
    monkeypatch.setattr(service, 'classify_turn', lambda *a, **kw: classification)
    h.create()
    metadata = h.conn.revisions[0]['metadata']
    assert metadata['turn_kind'] == 'confirmed'
    assert set(metadata) == {'source_message_ids', 'composer_draft', 'turn_kind', 'decisions', 'turn_kind_evidence'}


@pytest.mark.parametrize('classification', [None, {}, {'turn_kind': 'open', 'decisions': None},
    '{"turn_kind":"open","decisions":[],"turn_kind_evidence":[],"unexpected":1}',
    'invalid json', [], 'null'])
def test_update_revision_handles_missing_classification_without_null_fields(monkeypatch, classification):
    h = Harness(monkeypatch)
    h.source = source()
    draft = h.create()
    current = h.conn.drafts[0]
    if classification == {}:
        current.pop('classification')
    else:
        current['classification'] = classification
    updated = asyncio.run(service.update_draft(
        tenant_id=TENANT_A, user_id=None, draft_id=draft['id'], title=None,
        content=draft['content'].replace('TITLE: ', 'TITLE: 수정 ', 1), expected_revision=1))
    metadata = h.conn.revisions[-1]['metadata']
    assert updated['current_revision'] == 2
    assert metadata['source_message_ids'] == draft['source_message_ids']
    assert all(v is not None for v in metadata.values())
    assert 'unexpected' not in metadata
    if isinstance(classification, dict) and classification.get('turn_kind'):
        assert metadata['turn_kind'] == classification['turn_kind']
    elif isinstance(classification, str) and classification.startswith('{'):
        assert metadata == {'source_message_ids': draft['source_message_ids'], 'turn_kind': 'open',
                            'decisions': [], 'turn_kind_evidence': []}
    else:
        assert set(metadata) == {'source_message_ids'}


def test_create_and_update_use_same_sparse_turn_metadata(monkeypatch):
    h = Harness(monkeypatch)
    h.source = source()
    monkeypatch.setattr(service, 'classify_turn', lambda *a, **kw: {
        'turn_kind': 'open', 'decisions': None, 'turn_kind_evidence': None, 'extra': 'omit'})
    draft = h.create()
    asyncio.run(service.update_draft(
        tenant_id=TENANT_A, user_id=None, draft_id=draft['id'], title=None,
        content=draft['content'].replace('TITLE: ', 'TITLE: 수정 ', 1), expected_revision=1))
    for revision in h.conn.revisions:
        metadata = revision['metadata']
        assert metadata['turn_kind'] == 'open'
        assert 'decisions' not in metadata and 'turn_kind_evidence' not in metadata
        assert 'extra' not in metadata


@pytest.mark.parametrize('field', ['goal_id', 'milestone_id'])
def test_resend_stays_inside_plan_binding(monkeypatch, field):
    h = Harness(monkeypatch)
    binding = {'goal_id': str(uuid.uuid4()), 'milestone_id': str(uuid.uuid4())}

    async def resolve(*args, **kwargs):
        return dict(binding)

    monkeypatch.setattr(service, '_resolve_goal_binding', resolve)
    h.source = source()
    first = h.create()
    h.source = source(10)
    binding[field] = str(uuid.uuid4())
    assert h.create()['id'] != first['id']
