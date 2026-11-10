"""Synthetic partial screens must never verify runtime authentication."""
import json

import pytest

from scripts.agy_usage import parse_usage

ACCOUNT = 'test@example.invalid'


def payload():
    return {'account': ACCOUNT, 'groups': [
        {'name': 'Gemini', 'models': ['gemini-test'], 'windows': [
            {'period': 'weekly', 'remaining_percent': 80, 'reset_at': None},
            {'period': 'five-hour', 'remaining_percent': 70, 'reset_at': None}]}]}


def partial_screens():
    header = f'Account: {ACCOUNT}\nGemini\nModels: gemini-test\n'
    return [header + 'five-hour: 80% remaining',
            header + 'weekly: 80% remaining',
            json.dumps(payload()) + '\nweekly: unavailable']


@pytest.mark.parametrize('screen', partial_screens())
def test_reported_partial_response_rejected(screen):
    with pytest.raises(ValueError):
        parse_usage(screen, ACCOUNT, [])


def test_complete_json_control():
    assert len(parse_usage(json.dumps(payload()), ACCOUNT, [])[0]['windows']) == 2


def invalid_screens():
    valid = json.dumps(payload())
    bad = json.dumps({'account': ACCOUNT, 'groups': []})
    screens = partial_screens() + [valid + '\nweekly: 80% remaining',
        'weekly: unavailable\n' + valid, valid + '\n' + bad,
        bad + '\n' + valid, valid + '\n' + valid,
        valid + '\nmonthly: 80% remaining', valid + '\n{broken json',
        'Account: other@example.invalid\n' + valid]
    for field, value in [('period', 'monthly'), ('remaining_percent', True),
                         ('remaining_percent', float('nan')),
                         ('remaining_percent', float('inf')), ('reset_at', True),
                         ('reset_at', 'tomorrow'), ('reset_at', '2026-11-10T00:00:00')]:
        item = payload()
        item['groups'][0]['windows'][0][field] = value
        screens.append(json.dumps(item))
    for name in ('Gemini', 'Claude and GPT'):
        for period in ('weekly', 'five-hour'):
            item = payload()
            second = {'name': name, 'models': ['synthetic-model'],
                      'windows': [{'period': period, 'remaining_percent': 50}]}
            if name == 'Gemini':
                item['groups'][0] = second
            else:
                item['groups'].append(second)
            screens.append(json.dumps(item))
    item = payload()
    item['groups'][0]['windows'].append(dict(item['groups'][0]['windows'][0]))
    screens.append(json.dumps(item))
    header = f'Account: {ACCOUNT}\nGemini\nModels: gemini-test\n'
    screens += [header + 'weekly: 80% remaining\nfive-hour: 70% remaining\nmonthly: unavailable',
                '\x1b[32m' + partial_screens()[0] + '\x1b[0m']
    return screens


@pytest.mark.parametrize('screen', invalid_screens())
def test_negative_formats_rejected(screen):
    with pytest.raises(ValueError):
        parse_usage(screen, ACCOUNT, [])


@pytest.mark.parametrize('decoration', ['', '/usage\n', '───\n', '\x1b[32m/usage\x1b[0m\n'])
def test_json_decoration_control(decoration):
    assert parse_usage(decoration + json.dumps(payload()) + '\n❯ ', ACCOUNT, [])


@pytest.mark.parametrize('screen', [s for s in invalid_screens() if s.startswith('{') and '\n' not in s])
def test_adapter_rejects_partial_groups_even_with_legacy_windows(screen):
    # The adapter must independently reject bad structured data from older relays.
    from datetime import datetime, timezone
    from app.core.agy_quota import _antigravity_usage
    data = json.loads(screen)
    binding = dict(data, quota_account=ACCOUNT, usage_status='available',
                   runtime_auth_status='verified', usage_source='agy_cli_usage',
                   usage_collected_at=datetime.now(timezone.utc).isoformat(),
                   windows=[{'window_minutes': 300, 'used_percent': 20,
                             'resets_at': '2026-11-11T00:00:00Z'}])
    assert _antigravity_usage(binding, ACCOUNT, 'ok', relay_available=True)['usage_status'] != 'available'


def duplicate_screens():
    valid = json.dumps(payload())
    replacements = [
        ('"account":', '"account":"other@example.invalid","account":'),
        ('"account":', '"account":"test@example.invalid","account":'),
        ('"groups":', '"groups":[],"groups":'),
        ('"name":', '"name":"Gemini","name":'),
        ('"models":', '"models":[],"models":'),
        ('"windows":', '"windows":[],"windows":'),
        ('"period":', '"period":"weekly","period":'),
        ('"remaining_percent":', '"remaining_percent":80,"remaining_percent":'),
        ('"reset_at":', '"reset_at":null,"reset_at":'),
        ('"account":', '"acc\\u006funt":"test@example.invalid","account":'),
    ]
    screens = [valid.replace(a, b, 1) for a, b in replacements]
    reversed_keys = []
    for field, value in [('account', ACCOUNT), ('groups', payload()['groups']),
                         ('name', 'Gemini'), ('models', ['gemini-test']),
                         ('windows', payload()['groups'][0]['windows']),
                         ('period', 'weekly'), ('remaining_percent', 80), ('reset_at', None)]:
        token = json.dumps(field) + ': ' + json.dumps(value)
        reversed_keys.append(valid.replace(token, token + ',' + token, 1))
    screens += reversed_keys
    return screens + [s + '\n' + valid for s in screens] + [valid + '\n' + s for s in screens]


@pytest.mark.parametrize('screen', duplicate_screens())
def test_r7_duplicate_keys_fail_closed(screen):
    with pytest.raises(ValueError):
        parse_usage(screen, ACCOUNT, [])


@pytest.mark.parametrize('screen', [
    json.dumps(payload()) + '\nAccount: other@example.invalid',
    'Account: other@example.invalid\n' + json.dumps(payload()),
    json.dumps(payload()) + '\n' + json.dumps(dict(payload(), account='other@example.invalid')),
    'Account: TEST@example.invalid\nAccount: other@example.invalid',
])
def test_r7_identity_precedes_format_error(screen):
    with pytest.raises(ValueError, match='quota_account_mismatch'):
        parse_usage(screen, ACCOUNT, [])


@pytest.mark.parametrize('level', ['root', 'group', 'window'])
@pytest.mark.parametrize('field', ['error', 'usage_status', 'login_required'])
def test_r7_failure_fields_any_depth_never_verify(level, field):
    value = payload()
    target = {'root': value, 'group': value['groups'][0],
              'window': value['groups'][0]['windows'][0]}[level]
    target[field] = 'collection_failed'
    with pytest.raises(ValueError):
        parse_usage(json.dumps(value), ACCOUNT, [])


@pytest.mark.parametrize('suffix', ['', '\n\x08'])
@pytest.mark.parametrize('reverse', [False, True])
def test_r7_duplicate_identity_order_and_case_keep_mismatch(suffix, reverse):
    identities = ['"account":"OTHER@example.invalid"', '"acc\\u006funt":"TEST@example.invalid"']
    if reverse:
        identities.reverse()
    value = json.dumps(payload()).replace('"account": "test@example.invalid"', ','.join(identities))
    with pytest.raises(ValueError, match='quota_account_mismatch'):
        parse_usage(value + suffix, ACCOUNT, [])


@pytest.mark.parametrize('screen', [
    json.dumps(payload()) + ' Account: other@example.invalid',
    'Account: other@example.invalid ' + json.dumps(payload()),
    'Account: test@example.invalid Account: other@example.invalid',
])
def test_r7_inline_mixed_identity_not_hidden_by_format_error(screen):
    with pytest.raises(ValueError, match='quota_account_mismatch'):
        parse_usage(screen, ACCOUNT, [])
