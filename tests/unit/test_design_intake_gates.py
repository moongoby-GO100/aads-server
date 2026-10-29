"""Intake confirmation and modification review rejection contracts."""
import json
import os
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ.setdefault('JWT_SECRET_KEY', 'test-secret')
from app.api import design_modifications as api
from app.services.design_agent import intake
from tests.unit.test_design_modifications_api import _DesignConn, _Pool


def connection():
    conn = AsyncMock()
    conn.transaction = lambda: nullcontext()
    conn.fetchrow.return_value = {'round': 1, 'id': 'question'}
    conn.fetchval.return_value = 0
    return conn


@pytest.mark.asyncio
async def test_two_examples_rejected():
    conn = connection()
    with pytest.raises(ValueError, match='3 ordinary'):
        await intake.save_question(conn, 'tenant', 'session', 'layout', 'Q',
                                   ['A', 'B', *intake.SPECIAL_OPTIONS])
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_unconfirmed_brief_rejected():
    conn = connection()
    conn.fetchval.return_value = None
    with pytest.raises(ValueError, match='brief must be confirmed'):
        await intake.assert_brief_confirmed(conn, 'tenant', 'session')


@pytest.mark.asyncio
@pytest.mark.parametrize('source', sorted(intake.ANSWER_SOURCES))
async def test_answer_sources_saved(source):
    conn = connection()
    stamp = datetime.now(timezone.utc)
    await intake.record_answer(conn, 'tenant', 'session', 'question', 'A', source, stamp)
    args = conn.fetchrow.call_args.args
    assert args[1:] == ('question', 'session', 'tenant', 'A', source, stamp)


@pytest.mark.asyncio
async def test_estimate_without_confirmation_cannot_confirm_brief():
    conn = connection()
    await intake.record_answer(conn, 'tenant', 'session', 'question', 'estimate', 'estimate_confirmed')
    assert conn.fetchrow.call_args.args[-1] is None
    conn.fetchrow.side_effect = [{'round': 1}, {'total': 1, 'confirmed': 0}]
    with pytest.raises(ValueError, match='confirmed answers'):
        await intake.confirm_brief(conn, 'tenant', 'session', {})
    query = conn.fetchrow.call_args.args[0]
    assert 'confirmed_at IS NOT NULL' in query


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['allowed_scope', 'forbidden_scope', 'acceptance_criteria'])
async def test_review_rejects_each_empty_scope(field):
    values = dict(allowed_scope=['header'], forbidden_scope=['footer'], acceptance_criteria=['readable'])
    values[field] = []
    payload = api.DesignModificationRequestCreate(project_key='AADS', user_prompt='change', status='review', **values)
    with pytest.raises(HTTPException) as error:
        await api.create_design_modification_request(payload, {'user_id': 'ceo'})
    assert error.value.status_code == 400
    assert field in error.value.detail


@pytest.mark.asyncio
async def test_review_with_scope_preserves_response(monkeypatch):
    monkeypatch.setattr(api, 'get_pool', lambda: _Pool(_DesignConn()))
    payload = api.DesignModificationRequestCreate(project_key='AADS', user_prompt='change', status='review',
        allowed_scope=['header'], forbidden_scope=['footer'], acceptance_criteria=['readable'])
    response = await api.create_design_modification_request(payload, {'user_id': 'ceo'})
    assert response.request.project_key == 'AADS'


@pytest.mark.asyncio
@pytest.mark.parametrize('round_number,count', [(4, 0), (1, 6)])
async def test_round_limits(round_number, count):
    conn = connection()
    conn.fetchrow.return_value = {'round': round_number}
    conn.fetchval.return_value = count
    with pytest.raises(ValueError):
        await intake.save_question(conn, 'tenant', 'session', 'area', 'Q', ['A', 'B', 'C'])


@pytest.mark.asyncio
async def test_special_options_appended():
    conn = connection()
    await intake.save_question(conn, 'tenant', 'session', 'area', 'Q', ['A', 'B', 'C'])
    assert all(option in json.loads(conn.fetchval.call_args.args[-2]) for option in intake.SPECIAL_OPTIONS)


@pytest.mark.asyncio
async def test_confirmed_answers_allow_brief_and_generation():
    conn = connection()
    stamp = datetime.now(timezone.utc)
    conn.fetchrow.side_effect = [{'round': 1}, {'total': 2, 'confirmed': 2}]
    conn.fetchval.return_value = stamp
    assert await intake.confirm_brief(conn, 'tenant', 'session', {'purpose': 'shop'}) == stamp
    await intake.assert_brief_confirmed(conn, 'tenant', 'session')
    assert conn.fetchval.call_args.args[1:] == ('session', 'tenant')


@pytest.mark.asyncio
async def test_invalid_source_and_cross_tenant_session_rejected():
    conn = connection()
    with pytest.raises(ValueError, match='answer_source'):
        await intake.record_answer(conn, 'tenant', 'session', 'question', 'A', 'estimated')
    conn.fetchrow.return_value = None
    with pytest.raises(ValueError, match='session not found'):
        await intake.save_question(conn, 'other-tenant', 'session', 'area', 'Q', ['A', 'B', 'C'])
    assert conn.fetchrow.call_args.args[1:] == ('session', 'other-tenant')
