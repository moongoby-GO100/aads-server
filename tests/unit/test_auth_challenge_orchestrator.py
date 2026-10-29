import importlib.util
import sys
from pathlib import Path


_ORCHESTRATOR_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "auth_challenge_orchestrator.py"
_SPEC = importlib.util.spec_from_file_location("auth_challenge_orchestrator", _ORCHESTRATOR_PATH)
orchestrator = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = orchestrator
_SPEC.loader.exec_module(orchestrator)

classify_portal_state = orchestrator.classify_portal_state
make_resume_token = orchestrator.make_resume_token
validate_provider_output = orchestrator.validate_provider_output


def test_classifies_challenges_without_solving_them():
    assert classify_portal_state("https://portal.test", "보안문자 입력").state == "captcha_required"
    assert classify_portal_state("https://portal.test", "인증번호를 입력").state == "otp_required"
    assert classify_portal_state("https://portal.test/login", "로그인").state == "login_required"
    assert classify_portal_state("https://portal.test", "인증서 비밀번호").state == "certificate_password_required"
    assert classify_portal_state("https://portal.test", "본인인증").state == "identity_check_required"
    assert classify_portal_state("https://portal.test", "일정시간 이상 서비스 이용 정보가 없습니다").state == "session_expired"
    assert classify_portal_state("https://portal.test", "조회된 거래내역이 없습니다").state == "no_records"
    assert classify_portal_state("https://portal.test", "거래일자 입금금액 출금금액").state == "transaction_table"


def test_provider_state_is_allowlisted_and_invalid_output_is_unknown():
    assert validate_provider_output({"state": "collectable_page", "evidence": ["table"]}).state == "collectable_page"
    assert validate_provider_output({"state": "solve_captcha"}).state == "unknown"
    assert validate_provider_output({"state": "login_required", "confidence": 0.2}).state == "unknown"
    assert validate_provider_output(
        {"state": "login_required", "confidence": 0.9, "suggested_action": "solve_captcha"}
    ).state == "unknown"


def test_resume_token_is_opaque_and_stable():
    token = make_resume_token("work", "session", "run")
    assert token == make_resume_token("work", "session", "run")
    assert len(token) == 32
    assert "work" not in token and "session" not in token


def test_durable_scope_never_accepts_challenge_values():
    import pytest
    with pytest.raises(ValueError, match='auth_scope_invalid'):
        orchestrator.validate_auth_scope({'otp': '123456'})


async def test_persistent_resume_cancel_expiry_and_cross_account(monkeypatch):
    import json
    import uuid
    from datetime import datetime, timedelta, timezone
    import pytest
    from app.core import db_pool
    from app.services import browser_task_gateway as gateway
    from app.services.work_recipe import dom_evidence

    now = datetime.now(timezone.utc)
    tenant = str(uuid.uuid4())
    scope = dict(tenant_id=tenant, business_ref='business', account_ref='account',
                 session_id=str(uuid.uuid4()), work_key='work', lane='server', target='https://example.com',
                 recipe_name='login', recipe_version=2, checkpoint=3,
                 expires_at=(now + timedelta(minutes=5)).isoformat())
    row = dict(id=str(uuid.uuid4()), tenant_id=tenant, user_id='user', status='waiting_for_user',
               updated_at=now, result={'auth_scope': scope, 'verify': [{'assertion': 'element_visible', 'selector': '#account'}]})
    locked = []
    class Context:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            locked.clear()
            return False
        def transaction(self):
            return self
        async def fetchrow(self, sql, *args):
            if 'FOR UPDATE' in sql:
                locked.append(True)
            if sql.startswith('UPDATE'):
                row.update(status=args[2], result=json.loads(args[3]))
            return dict(row)
    class Pool:
        def acquire(self):
            return Context()
    async def evidence(**kwargs):
        assert not locked, 'evidence must be verified before FOR UPDATE'
        assert kwargs['verify'] == row['result']['verify']
        return {'artifact_id': kwargs['claimed']['artifact_id'], 'lane': 'server', 'captured_at': (now + timedelta(seconds=1)).isoformat()}
    async def event(**kwargs):
        pass
    monkeypatch.setattr(db_pool, 'get_pool', lambda: Pool())
    monkeypatch.setattr(gateway, '_task_to_dict', dict)
    monkeypatch.setattr(gateway, 'append_browser_task_event', event)
    monkeypatch.setattr(dom_evidence, 'verify_dom_evidence', evidence)
    args = dict(tenant_id=tenant, task_id=row['id'], user_id='user', scope=scope, action='resume', artifact_id='fresh')
    with pytest.raises(ValueError, match='scope_mismatch'):
        await orchestrator.transition_auth_wait(**{**args, 'scope': {**scope, 'account_ref': 'other'}})
    result = await orchestrator.transition_auth_wait(**args)
    assert result['status'] == 'queued'
    with pytest.raises(ValueError, match='already_consumed'):
        await orchestrator.transition_auth_wait(**args)
    row['status'] = 'waiting_for_user'
    assert (await orchestrator.transition_auth_wait(**{**args, 'action': 'cancel'}))['status'] == 'cancelled'
    row['status'] = 'waiting_for_user'
    expired_scope = {**scope, 'expires_at': (now - timedelta(seconds=1)).isoformat()}
    row['result']['auth_scope'] = expired_scope
    assert (await orchestrator.transition_auth_wait(**{**args, 'scope': expired_scope}))['status'] == 'expired'


async def test_status_patch_none_result_and_auth_guard(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from fastapi import HTTPException
    from app.api import browser_tasks as api
    from unittest.mock import AsyncMock
    monkeypatch.setattr(api, '_tenant_id', lambda context: 'tenant')
    get = AsyncMock(return_value={'result': {}})
    update = AsyncMock(return_value={'id': 'task'})
    monkeypatch.setattr(api, 'get_browser_task', get)
    monkeypatch.setattr(api, 'update_browser_task_status', update)
    async def display(value, context): return value
    monkeypatch.setattr(api, '_display', display)
    body = api.BrowserTaskStatusPatch.model_construct(status='running', result=None)
    assert (await api.api_update_browser_task_status('task', body, SimpleNamespace()))['status'] == 'updated'
    get.assert_awaited_once()
    assert update.await_args.kwargs['result'] is None
    get.reset_mock()
    update.reset_mock()
    get.return_value = {'result': {'auth_scope': {'account_ref': 'account'}}}
    with pytest.raises(HTTPException) as error:
        await api.api_update_browser_task_status('task', body, SimpleNamespace())
    assert error.value.status_code == 409
    get.assert_awaited_once()
    update.assert_not_awaited()
