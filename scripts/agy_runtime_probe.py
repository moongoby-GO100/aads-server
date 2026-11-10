#!/usr/bin/env python3
"""AGY 실제 인증 읽기 전용 검증. 원문 화면·계정·토큰·예외는 출력하지 않는다.

116에서 검토한 작업트리를 --repo로 지정한다. 새 로그인/설정 쓰기는 하지 않는다.
기존 relay PID의 허용 환경만 사용하며 CLI 출력은 메모리에서만 검사한다.
실제 /usage 성공 없이는 운영검증 성공(exit 0)을 반환하지 않는다.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
from pathlib import Path
import sys
import time
import urllib.request

ENV_KEYS = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TZ', 'XDG_CONFIG_HOME',
            'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'AGY_BIN')


def identity(value):
    return hashlib.sha256(str(value).casefold().encode()).hexdigest()[:16] if value else None


def observation(value):
    # Explicit allow-list: never serialize arbitrary CLI, relay or API fields.
    return {
        'account_ref': identity(value.get('quota_account') or value.get('actual_account') or value.get('account')),
        'bound': value.get('bound'), 'needs_login': value.get('needs_login'),
        'state': value.get('state'), 'runtime_auth_status': value.get('runtime_auth_status'),
        'usage_status': value.get('usage_status'), 'usage_source': value.get('usage_source'),
        'usage_collected_at': value.get('usage_collected_at'),
        'groups': [{
            'name': g.get('name') if g.get('name') in {'Gemini', 'Claude and GPT'} else 'unknown',
            'windows': [{**{k: w.get(k) for k in (
                'period', 'remaining_percent', 'used_percent', 'reset_at')},
                'stale': bool(w.get('stale'))}
                for w in g.get('windows', [])],
        } for g in value.get('groups', [])],
    }


def get_json(url, headers):
    start = time.monotonic()
    # Do not forward authentication to redirects or ambient HTTP proxies.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return json.load(response), {'ok': True, 'elapsed_ms': round((time.monotonic() - start) * 1000, 2)}
    except Exception:
        return {}, {'ok': False, 'elapsed_ms': round((time.monotonic() - start) * 1000, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--relay-pid', type=int, required=True)
    parser.add_argument('--relay-url', default='http://127.0.0.1:8199')
    parser.add_argument('--api-url', default='http://127.0.0.1:8080')
    args = parser.parse_args()
    # Authentication headers may only go to host-local services.
    from urllib.parse import urlsplit
    for base in (args.relay_url, args.api_url):
        parts = urlsplit(base)
        if parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', 'localhost', '::1'} or parts.username or parts.query or parts.fragment:
            parser.error('local HTTP service URL required')
    sys.path.insert(0, str(args.repo.resolve()))
    from scripts import account_login
    from scripts.agy_usage import LoginRequiredError, parse_usage, read_usage
    from scripts import claude_relay_server as relay
    raw = Path(f'/proc/{args.relay_pid}/environ').read_bytes().split(b'\0')
    process_env = {}
    for entry in raw:
        key, sep, value = entry.partition(b'=')
        if sep and key.decode(errors='replace') in ENV_KEYS:
            process_env[key.decode()] = value.decode()
    del raw
    if process_env.get('HOME') != os.environ.get('HOME'):
        print(json.dumps({'ok': False, 'reason': 'probe_home_differs_from_relay'}))
        return 2
    # Use only documented execution environment; never guess auth overrides.
    binding = account_login._antigravity_binding()
    configured_binary = process_env.pop('AGY_BIN', '').strip()
    binary = str(Path(configured_binary).expanduser()) if configured_binary else shutil.which('agy', path=process_env.get('PATH', '/usr/local/bin:/usr/bin:/bin'))
    if not binary:
        binary = next((p for p in ('/usr/local/bin/agy', '/root/.local/bin/agy') if Path(p).is_file()), '')
    credential = account_login.ANTIGRAVITY_HOME_ROOT / 'antigravity-cli' / 'antigravity-oauth-token'
    result = {'ok': False, 'environment_keys': sorted(process_env),
              'relay_process_ref': args.relay_pid,
              'probe_repo': str(args.repo.resolve()),
              'binary': str(Path(binary).resolve()) if binary else None,
              'metadata': observation(binding),
              'binding_credential_reference': str(credential),
              'binding_credential_exists': credential.is_file(),
              'credential_path_cause': 'unresolved_requires_installed_cli_source_review',
              'model_response_latency': 'unverified_no_generation_calls'}
    started = time.monotonic()
    try:
        groups = parse_usage(read_usage(binary, process_env, 12), binding.get('account'), [])
        result['cli'] = observation({'quota_account': binding.get('account'),
                                     'usage_status': 'available', 'groups': groups,
                                     'usage_source': 'agy_cli_usage',
                                     'usage_collected_at': datetime.now(timezone.utc).isoformat()})
    except LoginRequiredError:
        result['cli'] = {'usage_status': 'login_required', 'groups': [], 'used_percent': None}
    except Exception:
        result['cli'] = {'usage_status': 'unknown', 'groups': [], 'used_percent': None}
    result['cli_elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
    headers = {}
    secret = relay._load_relay_secret()
    if secret:
        headers['X-Claude-Relay-Secret'] = secret
    payload, timing = get_json(args.relay_url + '/account-bindings', headers)
    result['relay_timing'] = timing
    result['relay'] = observation(next((b for b in payload.get('bindings', []) if b.get('kind') == 'antigravity'), {}))
    _, result['codex_usage_timing'] = get_json(args.relay_url + '/codex-usage', headers)
    api_headers = {}
    if os.environ.get('AADS_MONITOR_KEY'):
        api_headers['x-monitor-key'] = os.environ['AADS_MONITOR_KEY']
    payload, result['api_timing'] = get_json(args.api_url + '/api/v1/llm-keys/overview', api_headers)
    result['api'] = observation(next((a for a in payload.get('accounts', []) if a.get('provider') == 'antigravity'), {}))
    result['account_matches'] = bool(result['cli'].get('account_ref') and
        result['cli']['account_ref'] == result['relay']['account_ref'] == result['api']['account_ref'])
    result['groups_match'] = bool(result['cli'].get('groups') and
        result['cli']['groups'] == result['relay']['groups'] == result['api']['groups'])
    result['ok'] = (result['cli']['usage_status'] == 'available' and
                    result['relay']['usage_status'] == 'available' and
                    result['api']['usage_status'] == 'available' and
                    result['account_matches'] and result['groups_match'])
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({'ok': False, 'reason': 'probe_failed_no_sensitive_details'}))
        raise SystemExit(2)
