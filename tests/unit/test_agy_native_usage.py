"""Captured CLI 1.5.4 grammar, with the account replaced by a test identity."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.agy_usage import parse_usage, QuotaIdentityError
from app.core.agy_quota import _antigravity_usage

ACCOUNT = 'test@example.invalid'
SCREEN = Path('tests/fixtures/agy/usage-modal-1.5.4.txt').read_text()


def test_live_modal_preserves_independent_pools_and_disabled_window():
    groups = parse_usage(SCREEN, ACCOUNT, [])
    assert len(groups) == 2
    assert groups[0]['windows'][0]['remaining_percent'] == 92.30
    assert groups[1]['windows'][0]['remaining_percent'] == 0
    assert groups[1]['windows'][1]['remaining_percent'] is None
    assert groups[1]['windows'][1]['disabled']
    binding = dict(groups=groups, bound=True, quota_account=ACCOUNT,
                   usage_status='available', runtime_auth_status='verified',
                   usage_source='agy_cli_usage', usage_collected_at=datetime.now(timezone.utc).isoformat())
    result = _antigravity_usage(binding, ACCOUNT, 'ok', relay_available=True)
    assert result['usage_status'] == 'available'
    assert result['groups'][1]['windows'][1]['used_percent'] is None
    assert result['groups'][0]['windows'][0]['reset_at'] is None


def test_each_truncated_native_line_is_rejected():
    lines = SCREEN.splitlines()
    for index in range(1, len(lines)):
        with pytest.raises((ValueError, IndexError)):
            parse_usage('\n'.join(lines[:index]), ACCOUNT, [])


def test_conflicting_identity_and_extra_windows_are_rejected():
    with pytest.raises(QuotaIdentityError):
        parse_usage(SCREEN, 'other@example.invalid', [])
    with pytest.raises(ValueError):
        parse_usage(SCREEN.replace('  │Within', 'Monthly Limit Remaining\n  │Within'), ACCOUNT, [])


def test_truncated_json_identity_evidence_invalidates_prior_cache():
    with pytest.raises(QuotaIdentityError, match='quota_account_mismatch'):
        parse_usage(json.dumps({'account': ACCOUNT, 'groups': []}) + '\n{"account":"other@example.invalid","groups":', ACCOUNT, [])
