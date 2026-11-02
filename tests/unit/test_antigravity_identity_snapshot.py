import json
from datetime import datetime, timedelta, timezone

import pytest

from app.api import llm_keys
from scripts import account_login


@pytest.fixture
def snapshot(monkeypatch, tmp_path):
    path = tmp_path / 'identity.json'
    monkeypatch.setattr(llm_keys, 'ANTIGRAVITY_IDENTITY_SNAPSHOT', path)
    return path


def write(path, **kw):
    payload = dict(schema_version=1, collected_at=datetime.now(timezone.utc).isoformat(),
                   account='agy@example.com', account_source='agy_credential_metadata')
    payload.update(kw)
    path.write_text(json.dumps(payload))


def test_live_relay_can_keep_running_old_code(snapshot):
    write(snapshot)
    old = {llm_keys.ANTIGRAVITY_BINDING_KEY: {'bound': True, 'needs_login': False, 'account': None}}
    acc = llm_keys._antigravity_account(old)
    assert acc['actual_account'] == 'agy@example.com'
    assert acc['account_source'] == 'agy_credential_metadata'
    assert acc['account_verified'] is False
    assert acc['usage_status'] == 'unsupported'
    assert acc['state'] == 'ok'


def test_identity_does_not_claim_live_auth_when_relay_down(snapshot):
    write(snapshot)
    acc = llm_keys._antigravity_account(None)
    assert acc['actual_account'] == 'agy@example.com'
    assert acc['state'] == 'unknown'
    assert acc['bound'] is False
    assert acc['account_verified'] is False


@pytest.mark.parametrize('kw', [
    {'collected_at': (datetime.now(timezone.utc)-timedelta(minutes=4)).isoformat()},
    {'collected_at': (datetime.now(timezone.utc)+timedelta(minutes=4)).isoformat()},
    {'collected_at': 'invalid'}, {'collected_at': '2026-01-01T00:00:00'},
    {'account': 'not-email'}, {'account_source': 'gemini_different_account'},
    {'schema_version': 2},
])
def test_invalid_projection_ignored(snapshot, kw):
    write(snapshot, **kw)
    assert llm_keys._antigravity_identity() is None


def test_oversized_or_malformed_projection_ignored(snapshot):
    snapshot.write_text('x'*4097)
    assert llm_keys._antigravity_identity() is None
    snapshot.write_text('[]')
    assert llm_keys._antigravity_identity() is None


def test_export_drops_secrets_and_does_not_leak_old_identity(monkeypatch, snapshot):
    monkeypatch.setattr(account_login, '_antigravity_binding', lambda: {
        'account': 'agy@example.com', 'account_source':'agy_credential_metadata',
        'access_token':'SECRET', 'refresh_token':'SECRET'})
    account_login.export_antigravity_identity(snapshot)
    exported = json.loads(snapshot.read_text())
    assert set(exported) == {'schema_version','collected_at','account','account_source'}
    assert 'SECRET' not in snapshot.read_text()
    assert snapshot.stat().st_mode & 0o777 == 0o640
    monkeypatch.setattr(account_login, '_antigravity_binding', lambda: {})
    account_login.export_antigravity_identity(snapshot)
    assert llm_keys._antigravity_identity() is None


def test_new_relay_credential_identity_wins_over_older_projection(snapshot):
    write(snapshot)
    acc = llm_keys._antigravity_account({llm_keys.ANTIGRAVITY_BINDING_KEY: {
        'account': 'new-login@example.com', 'account_source': 'agy_credential_metadata',
        'bound': True, 'needs_login': False}})
    assert acc['actual_account'] == 'new-login@example.com'
