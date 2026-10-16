"""Approval display profiles and additive GET contract."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.routers import goals
from app.services.approval_profiles import profile_for_project, profile_options
from app.services import goal_approval_policy_schema as schema


@pytest.mark.parametrize('project', ['GO100', 'KIS', ' go100 ', 'kis'])
def test_trading_keeps_existing_labels(project):
    assert profile_for_project(project) == 'trading'
    options = profile_options('trading')
    assert options['auto_approve_high']['label'] == '실매매 코드 수정 미리 승인'
    assert options['auto_approve_critical']['label'] == '주문·자금까지 미리 승인'
    assert '주문·자금·서비스 재기동' in options['auto_approve_critical']['description']


@pytest.mark.parametrize('project', ['AADS', 'NTV2', 'ACCT', '', None, ' ', 'GO100-test'])
def test_general_profile(project):
    assert profile_for_project(project) == 'general'
    options = profile_options('general')
    assert options['auto_approve_high']['label'] == '다음 단계 자동 진행(고위험 제외)'
    assert options['auto_approve_critical']['label'] == '고위험 단계까지 자동 진행'
    for option in options.values():
        assert '코드·도구 권한을 새로 주는 것이 아니라 다음 단계 제안 카드 승인을 생략' in option['description']
        assert '허용 횟수' in option['description']
    assert options['auto_approve_high']['risk_tone'] == 'warning'
    assert options['auto_approve_critical']['risk_tone'] == 'danger'


def test_options_are_independent_and_unknown_profile_is_general():
    options = profile_options('general')
    options['auto_approve_high']['label'] = 'changed'
    assert profile_options('unknown') == profile_options('general')


@pytest.mark.parametrize('project', ['GO100', 'KIS', 'AADS', 'NTV2', 'ACCT', '', None])
@pytest.mark.parametrize('serialized', [False, True])
def test_get_preserves_existing_contract(monkeypatch, project, serialized):
    from app.core import db_pool

    policy = dict(auto_approve_high=True, auto_approve_critical=False,
                  max_executions=200, used=12, set_by='CEO', set_at='2026-10-01',
                  legacy_key='unchanged')
    db = AsyncMock()
    db.fetchrow.return_value = dict(p=json.dumps(policy) if serialized else policy,
                                   project=project, title='goal', status='active')
    monkeypatch.setattr(db_pool, 'get_pool', lambda: db)
    result = asyncio.run(goals.get_goal_approval_policy(
        '11111111-1111-4111-8111-111111111111',
        {'tenant': {'id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}},
    ))
    expected = {k: policy[k] for k in (
        'auto_approve_high', 'auto_approve_critical', 'max_executions', 'used', 'set_by', 'set_at')}
    expected.update(goal_id='11111111-1111-4111-8111-111111111111', title='goal', status='active',
                    classification=schema.classify_policy(policy),
                    effective_summary=schema.effective_summary(policy), ignored_keys=['legacy_key'])
    assert {k: v for k, v in result.items() if k not in {'profile', 'options'}} == expected
    assert result['profile'] == profile_for_project(project)
    assert result['options'] == profile_options(result['profile'])
    query, *args = db.fetchrow.call_args.args
    assert 'status, project' in query
    assert 'tenant_id = $2::uuid' in query
    assert args[-1] == 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    assert policy['legacy_key'] == 'unchanged'


def test_get_missing_goal_remains_404(monkeypatch):
    from app.core import db_pool

    db = AsyncMock()
    db.fetchrow.return_value = None
    monkeypatch.setattr(db_pool, 'get_pool', lambda: db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.get_goal_approval_policy('missing', {'tenant': {'id': 'tenant'}}))
    assert exc.value.status_code == 404
