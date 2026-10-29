"""Regression coverage for API and chat scope gate bypasses."""
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests.unit.test_design_modifications_api import _DesignConn, _Pool, REQUEST_ID
from app.api import design_modifications as api


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['ready', 'approved', 'running', 'review'])
@pytest.mark.parametrize('empty', [[], {'notes': []}, {'notes': ['  ']}])
async def test_active_status_rejects_empty_scope(status, empty):
    payload = api.DesignModificationRequestCreate(
        project_key='AADS', user_prompt='change', status=status,
        allowed_scope=empty, forbidden_scope=empty, acceptance_criteria=[])
    with pytest.raises(HTTPException) as error:
        await api.create_design_modification_request(payload, {'user_id': 'ceo'})
    assert error.value.status_code == 400
    assert error.value.detail == (
        f'allowed_scope, forbidden_scope, acceptance_criteria is required for {status}')


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['ready', 'draft', 'rejected'])
async def test_allowed_creation(monkeypatch, status):
    monkeypatch.setattr(api, 'get_pool', lambda: _Pool(_DesignConn()))
    empty = status != 'ready'
    payload = api.DesignModificationRequestCreate(
        project_key='AADS', user_prompt='change', status=status,
        allowed_scope={'notes': [] if empty else ['헤더만']},
        forbidden_scope={'notes': [] if empty else ['푸터 금지']},
        acceptance_criteria=[] if empty else ['확인'])
    response = await api.create_design_modification_request(payload, {'user_id': 'ceo'})
    assert response.request.status == status


@pytest.mark.parametrize('value,missing', [
    (None, True), (False, True), (0, True), ('  ', True), ('[]', True),
    ('{"notes": []}', True), ('{"notes": ["  "]}', True),
    ({'notes': [], 'other': ['filled']}, True), ({'other': []}, True),
    ({'other': ['filled']}, False), ('plain text', False),
    ('"text"', False), ('null', True), ('false', True),
    ('{"notes": ["filled"]}', False), ({'notes': '  '}, True),
])
def test_scope_shapes(value, missing):
    assert api.missing_design_scope_fields(value, ['ok'], ['ok']) == (
        ['allowed_scope'] if missing else [])


@pytest.mark.asyncio
@pytest.mark.parametrize('value,expected', [
    ([], 'draft'), ({'notes': []}, 'draft'), ({'notes': ['  ']}, 'draft'),
    ('[]', 'draft'), (['헤더만'], 'ready'), ({'notes': ['헤더만']}, 'ready'),
])
async def test_chat_persists_gated_status(monkeypatch, value, expected):
    from app.core import db_pool
    from app.services.tool_executor import ToolExecutor

    conn = AsyncMock()

    async def inserted(query, *args):
        assert 'INSERT INTO design_modification_requests' in query
        assert '$8::jsonb, $9' in query
        assert args[8] == expected
        assert isinstance(json.loads(args[5]), dict)
        return {'id': REQUEST_ID, 'status': args[8]}

    conn.fetchrow.side_effect = inserted
    monkeypatch.setattr(db_pool, 'get_pool', lambda: _Pool(conn))
    result = await ToolExecutor()._create_design_modification_request({
        'user_prompt': 'change', 'allowed_scope': value, 'forbidden_scope': value,
        'acceptance_criteria': [] if expected == 'draft' else ['확인'],
        'build_context': False,
    })
    assert result['status'] == 'created'
    assert result['request']['status'] == expected
    assert {'context_pack', 'urls'} <= result.keys()
    if expected == 'draft':
        assert result['missing_fields'] == ['allowed_scope', 'forbidden_scope', 'acceptance_criteria']
        assert '초안' in result['draft_reason']
    else:
        assert 'missing_fields' not in result
        assert 'draft_reason' not in result
