"""Synthetic quota observations; these fixtures do not establish live CLI support."""
from datetime import datetime, timedelta, timezone

import pytest


from app.core.agy_quota import _antigravity_groups, _antigravity_usage


def adapter():
    return _antigravity_groups

def observation(value=50):
    return {'quota_account': 'a@example.com', 'usage_status': 'available', 'usage_source': 'agy_cli_usage',
            'usage_collected_at': datetime.now(timezone.utc).isoformat(),
            'groups': [{'name': name, 'models': models,
                        'windows': [{'period': period, 'remaining_percent': value}
                                    for period in ('weekly', 'five-hour')]} for name, models in
                       [('Gemini', ['gemini-model']), ('Claude and GPT', ['claude-model', 'gpt-model'])]]}


@pytest.mark.parametrize('value', [0, 50, 100])
def test_independent_groups_and_boundary_conversion(value):
    groups = adapter()(observation(value), 'a@example.com', 'ok')
    assert len(groups) == 2
    for group in groups:
        window = group['windows'][0]
        assert window['used_percent'] == 100 - value
        assert window['remaining_percent'] == value
        assert window['reset_at'] is None


@pytest.mark.parametrize('value', [None, True, -1, 101, float('nan'), float('inf'), '50'])
def test_invalid_percent_is_unknown(value):
    assert adapter()(observation(value), 'a@example.com', 'ok') == []


def test_account_switch_and_expired_auth_do_not_reuse_observation():
    assert adapter()(observation(), 'b@example.com', 'ok') == []
    assert adapter()(observation(), 'a@example.com', 'needs_login') == []


@pytest.mark.parametrize('status', ['available', 'collection_failed', 'unknown'])
def test_stale_observation_is_null_with_timestamp(status):
    binding = observation()
    binding['usage_status'] = status
    binding['usage_collected_at'] = (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()
    window = adapter()(binding, 'a@example.com', 'ok')[0]['windows'][0]
    assert window['stale'] and window['used_percent'] is None
    assert window['remaining_percent'] is None and window['collected_at']


def test_future_or_empty_observation_rejected():
    assert adapter()({}, 'a@example.com', 'ok') == []
    binding = observation()
    binding['usage_collected_at'] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert adapter()(binding, 'a@example.com', 'ok') == []


def test_unattributed_source_is_not_a_measurement():
    binding = observation()
    binding.pop('usage_source')
    assert adapter()(binding, 'a@example.com', 'ok') == []


@pytest.mark.parametrize('status', ['available', 'collection_failed'])
def test_full_adapter_relay_failure_redacts_fresh_observation(status):
    binding = observation()
    binding['usage_status'] = status
    result = _antigravity_usage(binding, 'a@example.com', 'ok', relay_available=False)
    assert result['usage_status'] == 'collection_failed'
    assert result['used_percent'] is None and result['usage_collected_at']
    for group in result['groups']:
        for window in group['windows']:
            assert window['stale']
            assert window['used_percent'] is None and window['remaining_percent'] is None


def test_full_adapter_groups_are_independent_not_a_scalar():
    result = _antigravity_usage(observation(), 'a@example.com', 'ok', relay_available=True)
    assert result['usage_status'] == 'available'
    assert len(result['groups']) == 2 and result['used_percent'] is None


def test_full_adapter_retains_null_stale_windows_on_collection_failure():
    binding = observation()
    binding['usage_status'] = 'collection_failed'
    for group in binding['groups']:
        group['windows'][0]['remaining_percent'] = None
    result = _antigravity_usage(binding, 'a@example.com', 'ok', relay_available=True)
    assert len(result['groups']) == 2 and result['usage_collected_at']
    assert all(g['windows'][0]['stale'] for g in result['groups'])


@pytest.mark.parametrize('available,state', [(True, 'ok'), (False, 'ok'), (True, 'needs_login')])
def test_api_wrapper_source_delegates_with_original_signature(available, state):
    # Source-level evidence only; the API import integration is tested separately.
    import ast
    import inspect
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / 'app/api/llm_keys.py'
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_antigravity_usage')
    namespace = {'_collect_antigravity_usage': _antigravity_usage}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    wrapper = namespace['_antigravity_usage']
    assert inspect.signature(wrapper) == inspect.signature(_antigravity_usage)
    binding = observation()
    assert wrapper(binding, 'a@example.com', state, relay_available=available) == _antigravity_usage(
        binding, 'a@example.com', state, relay_available=available)


@pytest.mark.parametrize('percent,status', [(35, 'stale'), (-1, 'collection_failed')])
def test_expired_legacy_observation_validates_before_expiry(percent, status):
    binding = {'quota_account': 'a@example.com', 'usage_status': 'available',
               'usage_collected_at': '2000-01-01T00:00:00Z',
               'windows': [{'window_minutes': 300, 'used_percent': percent,
                            'resets_at': '2000-01-01T05:00:00Z'}]}
    result = _antigravity_usage(binding, 'a@example.com', 'ok', relay_available=True)
    assert result['usage_status'] == status
    assert result['used_percent'] is None and result['windows'] == []


def test_unknown_status_and_failure_provenance_survive_api_adapter():
    result = _antigravity_usage({'usage_status': 'unknown'}, 'a@example.com',
                               'ok', relay_available=True)
    assert result['usage_status'] == 'unknown'
    assert result['quota_account'] is None  # No measured identity may be inferred from UI email.
    assert result['usage_source'] == 'agy_cli_usage'
    assert result['usage_collected_at'] is None and result['used_percent'] is None
