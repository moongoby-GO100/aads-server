"""Module-level collector integration; screen fixtures are synthetic, not live evidence."""
import asyncio
import json
import threading
import time

import pytest

from app.core.agy_quota import _antigravity_usage
from scripts.agy_usage import UsageCollector, parse_usage, read_usage

ACCOUNT = 'a@example.com'
BINDING = {'account': ACCOUNT, 'bound': True, 'needs_login': False}
ENV = {'HOME': '/relay/home'}


def run_async(coro):
    # Own a loop so synchronous tests can exercise real executor I/O without
    # asyncio.run's separate default-executor shutdown thread on this runner.
    # A timer also services executor callbacks where sandbox socket wakeups fail.
    loop = asyncio.new_event_loop()
    def pulse():
        loop.call_later(.005, pulse)
    pulse()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def screen(account=ACCOUNT, remaining=50):
    return json.dumps({'account': account, 'groups': [
        {'name': 'Gemini', 'models': ['gemini-model-high'], 'windows': [
            {'period': 'weekly', 'remaining_percent': remaining},
            {'period': 'five-hour', 'remaining_percent': remaining}]},
        {'name': 'Claude and GPT', 'models': ['claude-model-high', 'gpt-model-medium'],
         'windows': [{'period': 'five-hour', 'remaining_percent': 0},
                     {'period': 'weekly', 'remaining_percent': 0}]}]})


def test_collector_singleflight_ttl_and_complete_adapter():
    calls = []
    def reader(*args):
        calls.append(args)
        time.sleep(.03)
        return screen()
    async def run():
        collector = UsageCollector(reader)
        results = await asyncio.gather(*(collector.get(BINDING, '/agy', ENV, []) for _ in range(20)))
        assert all(r == results[0] for r in results)
        results[0]['groups'].clear()
        cached = await collector.get(BINDING, '/agy', ENV, [])
        assert len(cached['groups']) == 2
        result = _antigravity_usage(cached, ACCOUNT, 'ok', relay_available=True)
        assert result['usage_status'] == 'available'
        assert result['groups'][0]['windows'][0]['used_percent'] == 50
        assert result['groups'][1]['windows'][0]['used_percent'] == 100
        assert result['used_percent'] is None
    run_async(run())
    assert len(calls) == 1


@pytest.mark.parametrize('failure', [TimeoutError(), ValueError(), ''])
def test_failure_backoff_and_previous_observation(failure):
    calls = []
    def reader(*args):
        calls.append(1)
        if len(calls) == 1:
            return screen()
        if isinstance(failure, Exception):
            raise failure
        return failure
    async def run():
        collector = UsageCollector(reader)
        measured = await collector.get(BINDING, '/agy', ENV, [])
        collector.next_attempt = 0
        failed = await collector.get(BINDING, '/agy', ENV, [])
        assert failed['usage_status'] == 'collection_failed'
        assert failed['usage_collected_at'] == measured['usage_collected_at']
        result = _antigravity_usage(failed, ACCOUNT, 'ok', relay_available=True)
        assert result['usage_status'] == 'collection_failed'
        assert len(result['groups']) == 2
        assert all(g['windows'][0]['remaining_percent'] is None for g in result['groups'])
        await collector.get(BINDING, '/agy', ENV, [])
        assert len(calls) == 2
    run_async(run())


def test_switch_during_inflight_cannot_publish_old_account():
    started, finish = threading.Event(), threading.Event()
    def reader(binary, env, timeout):
        if binary == '/old':
            started.set()
            finish.wait(1)
            return screen()
        return screen('b@example.com', 100)
    async def run():
        collector = UsageCollector(reader)
        old = asyncio.create_task(collector.get(BINDING, '/old', ENV, []))
        await asyncio.to_thread(started.wait, 1)
        new = await collector.get(dict(BINDING, account='b@example.com'), '/new', ENV, [])
        finish.set()
        assert (await old)['usage_status'] == 'unknown'
        assert new['quota_account'] == 'b@example.com'
        assert collector.cached == new
    run_async(run())


def test_logout_invalidates_even_same_account_cache():
    async def run():
        collector = UsageCollector(lambda *args: screen())
        await collector.get(BINDING, '/agy', ENV, [])
        result = await collector.get(dict(BINDING, bound=False), '/agy', ENV, [])
        assert result['usage_status'] == 'unknown' and result['groups'] == []
        assert collector.cached is None
    run_async(run())


@pytest.mark.parametrize('value', [None, True, -1, 101, float('nan'), float('inf')])
def test_parser_rejects_invalid_percent(value):
    with pytest.raises(ValueError):
        parse_usage(screen(remaining=value), ACCOUNT, [])


@pytest.mark.parametrize('value', [0, 100])
def test_parser_boundaries_and_no_fabricated_reset(value):
    groups = parse_usage(screen(remaining=value), ACCOUNT, [])
    assert groups[0]['windows'][0]['used_percent'] == 100 - value
    assert groups[0]['windows'][0]['reset_at'] is None


def test_text_screen_requires_explicit_membership():
    text = 'Account: a@example.com\nGemini\nModels: gemini-a-high\nweekly: 75% remaining\nfive-hour: 70% remaining\nClaude and GPT\nModels: claude-a-high, gpt-a-medium\nfive-hour: 0% remaining\nweekly: 10% remaining'
    groups = parse_usage(text, ACCOUNT, [])
    assert len(groups) == 2 and groups[0]['windows'][0]['used_percent'] == 25
    with pytest.raises(ValueError):
        parse_usage('Gemini\nweekly: 50% remaining', ACCOUNT, [])


def test_pty_only_sends_slash_usage(tmp_path):
    # Exercise real PTY/process I/O without a provider or generation call.
    binary = tmp_path / 'fake-agy'
    binary.write_text('#!/usr/bin/python3\nimport sys\nprint("Type /help for commands", flush=True)\ncommand = input()\nassert command == "/usage"\nprint(' + repr(screen()) + ', flush=True)\nprint("> ", flush=True)\n')
    binary.chmod(0o700)
    result = read_usage(str(binary), ENV, timeout=1)
    assert parse_usage(result, ACCOUNT, [])[0]['name'] == 'Gemini'


def test_restart_recovers_catalog_before_first_execution_and_concurrent_requests():
    from scripts.agy_catalog import ensure_model
    async def run():
        accepted = {'antigravity': 'existing-high'}
        calls = []
        async def refresh():
            calls.append(1)
            await asyncio.sleep(.01)
            accepted.update({'new-concrete-high': 'new-concrete-high'})
        lock = asyncio.Lock()
        await asyncio.gather(*(ensure_model('new-concrete-high', accepted, refresh, lock) for _ in range(10)))
        assert accepted['new-concrete-high'] == 'new-concrete-high'
        assert len(calls) == 1
        await ensure_model('unlisted-high', accepted, refresh, lock)
        assert 'unlisted-high' not in accepted
    run_async(run())


def test_response_budget_keeps_singleflight_running():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        collector = UsageCollector()
        calls = []
        async def collect(identity, epoch, binary, env, catalog):
            calls.append(epoch)
            started.set()
            await release.wait()
            return {'usage_status': 'available', 'groups': []}
        collector._collect = collect
        first = await collector.get(BINDING, '/agy', ENV, [], wait_timeout=.01)
        assert first['usage_status'] == 'collection_failed'
        task = collector.task
        assert started.is_set() and not task.done()
        second = await collector.get(BINDING, '/agy', ENV, [], wait_timeout=.01)
        assert second['quota_account'] == ACCOUNT
        assert collector.task is task and len(calls) == 1
        release.set()
        await task
    run_async(scenario())


@pytest.mark.parametrize('identity', ['', 'Account: other@example.com\n',
                                      'Account: a@example.com\nAccount: a@example.com\n'])
def test_text_identity_fails_closed(identity):
    with pytest.raises(ValueError):
        parse_usage(identity + 'Gemini\nModels: gemini-a-high\nweekly: 75% remaining', ACCOUNT, [])


def test_initial_failure_keeps_provenance_and_backoff():
    def reader(*args):
        raise TimeoutError('startup')
    async def run():
        collector = UsageCollector(reader)
        result = await collector.get(BINDING, '/agy', ENV, [])
        assert result == await collector.get(BINDING, '/agy', ENV, [])
        assert result['usage_source'] == 'agy_cli_usage'
        assert result['quota_account'] == ACCOUNT
        assert result['usage_collected_at'] is None
        assert result['usage_status'] == 'collection_failed'
        assert result['used_percent'] is None
    run_async(run())


def test_cooperative_switch_and_logout_wait_for_reader_cleanup():
    started = threading.Event()
    state = {'active': 0, 'peak': 0, 'finished': []}
    def reader(binary, env, timeout, *, cancel_event):
        state['active'] += 1
        state['peak'] = max(state['peak'], state['active'])
        try:
            if binary == '/a':
                started.set()
                assert cancel_event.wait(1)
                raise InterruptedError()
            time.sleep(.02)
            return screen(binary[1:] + '@example.com')
        finally:
            state['active'] -= 1
            state['finished'].append(binary)
    async def run():
        collector = UsageCollector(reader)
        a = asyncio.create_task(collector.get(BINDING, '/a', ENV, []))
        while not started.is_set():
            await asyncio.sleep(.005)
        b = asyncio.create_task(collector.get(dict(BINDING, account='b@example.com'), '/b', ENV, []))
        await asyncio.sleep(.005)
        c_binding = dict(BINDING, account='c@example.com')
        c = await asyncio.gather(*(collector.get(c_binding, '/c', ENV, []) for _ in range(10)))
        assert all(r['quota_account'] == 'c@example.com' for r in c)
        assert (await a)['usage_status'] == 'unknown'
        assert (await b)['usage_status'] == 'unknown'
        result = await collector.get(dict(c_binding, bound=False), '/c', ENV, [])
        assert result['usage_status'] == 'unknown' and not result['groups']
        assert state['active'] == 0 and state['peak'] == 1
    run_async(run())


def test_delayed_start_split_ansi_ready_and_usage(tmp_path):
    binary = tmp_path / 'fake-agy'
    binary.write_text('#!/usr/bin/python3\nimport os, sys, time, select, termios\n'
                      'time.sleep(.08)\n'
                      '# Discard anything sent before initialization completes.\n'
                      'termios.tcflush(0, termios.TCIFLUSH)\n'
                      'sys.stdout.write("\\x1b[32mType /he"); sys.stdout.flush()\n'
                      'time.sleep(.03)\n'
                      'print("lp for commands\\x1b[0m", flush=True)\n'
                      'assert input() == "/usage"\n'
                      'print(' + repr(screen()) + ', flush=True)\nprint("> ", flush=True)\n')
    binary.chmod(0o700)
    assert parse_usage(read_usage(str(binary), ENV, .8), ACCOUNT, [])


def test_unknown_startup_never_sends_command_and_cleans_up(tmp_path):
    binary = tmp_path / 'fake-agy'
    marker = tmp_path / 'input-received'
    binary.write_text('#!/usr/bin/python3\nimport sys\n'
                      'print("Login required", flush=True)\n'
                      'input()\nopen(' + repr(str(marker)) + ', "w").write("unexpected")\n')
    binary.chmod(0o700)
    start = time.monotonic()
    with pytest.raises(TimeoutError, match='not_ready'):
        read_usage(str(binary), ENV, .3)
    assert time.monotonic() - start < .6
    assert not marker.exists()


def test_parent_exit_still_cleans_owned_child(tmp_path):
    import os
    from pathlib import Path
    binary = tmp_path / 'fake-agy'
    pidfile = tmp_path / 'child.pid'
    binary.write_text('#!/usr/bin/python3\nimport os, signal, time\n'
                      'pid=os.fork()\n'
                      'if pid == 0:\n'
                      ' signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                      ' open(' + repr(str(pidfile)) + ', "w").write(str(os.getpid()))\n'
                      ' while True: time.sleep(.01)\n'
                      'else:\n'
                      ' print("Type /help for commands", flush=True)\n'
                      ' assert input() == "/usage"\n'
                      ' print(' + repr(screen()) + ', flush=True)\n'
                      ' os._exit(0)\n')
    binary.chmod(0o700)
    start = time.monotonic()
    from scripts.agy_usage import CleanupIncompleteError
    with pytest.raises((TimeoutError, CleanupIncompleteError)):
        read_usage(str(binary), ENV, 1.5)
    assert time.monotonic() - start < 2.0
    pid = int(pidfile.read_text())
    # Grandchild zombies belong to the host init/subreaper; no running child
    # may remain. The Popen leader itself is reaped by read_usage.
    path = Path(f'/proc/{pid}/stat')
    assert not path.exists() or path.read_text().split()[2] == 'Z'


def test_real_pty_cancel_switch_logout_never_overlaps(tmp_path, monkeypatch):
    import subprocess
    binary = tmp_path / 'fake-agy'
    binary.write_text('#!/usr/bin/python3\nimport time\n'
                      'print("Type /help for commands", flush=True)\n'
                      'assert input() == "/usage"\n'
                      'time.sleep(2)\n')
    binary.chmod(0o700)
    owned = []
    popen = subprocess.Popen
    def launch(*args, **kwargs):
        assert all(p.poll() is not None for p in owned)
        proc = popen(*args, **kwargs)
        owned.append(proc)
        return proc
    monkeypatch.setattr(subprocess, 'Popen', launch)
    async def run():
        collector = UsageCollector(timeout=.5)
        first = asyncio.create_task(collector.get(BINDING, str(binary), ENV, []))
        while not owned:
            await asyncio.sleep(.005)
        second = asyncio.create_task(collector.get(dict(BINDING, account='b@example.com'), str(binary), ENV, []))
        await asyncio.sleep(.02)
        c = dict(BINDING, account='c@example.com')
        third = asyncio.create_task(collector.get(c, str(binary), ENV, []))
        while len(owned) < 2:
            await asyncio.sleep(.005)
        await collector.get(dict(c, bound=False), str(binary), ENV, [])
        results = await asyncio.gather(first, second, third)
        assert all(r['usage_status'] == 'unknown' and r['groups'] == [] for r in results)
        assert all(p.poll() is not None for p in owned)
    run_async(run())


@pytest.mark.parametrize('tail, reason', [
    ('time.sleep(2)', 'incomplete'),
    ('print("x" * 140000, flush=True); time.sleep(2)', 'limit'),
])
def test_partial_parseable_screen_never_available(tmp_path, tail, reason):
    binary = tmp_path / 'partial-agy'
    binary.write_text('#!/usr/bin/python3\nimport time\n'
                      'print("Type /help for commands", flush=True)\n'
                      'assert input() == "/usage"\n'
                      'print(' + repr(screen()) + ', flush=True)\n' + tail + '\n')
    binary.chmod(0o700)
    async def run():
        collector = UsageCollector(timeout=2 if reason == 'limit' else .5)
        result = await collector.get(BINDING, str(binary), ENV, [])
        assert result['usage_status'] == 'collection_failed'
        assert result['used_percent'] is None and result['remaining_percent'] is None
        assert reason in result['usage_note']
        assert collector.cached['usage_status'] != 'available'
    run_async(run())


def test_cleanup_failure_quarantines_all_later_accounts():
    from scripts.agy_usage import CleanupIncompleteError
    calls = []
    def reader(*args, **kwargs):
        calls.append(1)
        raise CleanupIncompleteError('quota_process_group_remaining')
    async def run():
        collector = UsageCollector(reader)
        for account in [ACCOUNT, 'b@example.com', 'c@example.com']:
            result = await collector.get(dict(BINDING, account=account), '/agy', ENV, [])
            assert result['usage_status'] == 'collection_failed'
        assert len(calls) == 1
    run_async(run())


def test_observe_latest_logout_cancels_without_followup_get():
    started, stopped = threading.Event(), threading.Event()
    def reader(binary, env, timeout, *, cancel_event):
        started.set()
        try:
            assert cancel_event.wait(1)
            raise InterruptedError('quota_collection_cancelled')
        finally:
            stopped.set()
    async def run():
        collector = UsageCollector(reader)
        pending = asyncio.create_task(collector.get(BINDING, '/agy', ENV, []))
        while not started.is_set():
            await asyncio.sleep(.005)
        collector.observe_binding(dict(BINDING, bound=False), ENV)
        assert (await pending)['usage_status'] == 'unknown'
        assert stopped.is_set() and collector.cached is None
    run_async(run())


def test_remaining_group_after_deadline_cannot_succeed_or_open_next_pty(tmp_path, monkeypatch):
    import os
    import signal
    from scripts.agy_usage import CleanupIncompleteError
    binary = tmp_path / 'prompt-agy'
    binary.write_text('#!/usr/bin/python3\nimport time\n'
                      'print("Type /help for commands", flush=True)\n'
                      'assert input() == "/usage"\n'
                      'print(' + repr(screen()) + ', flush=True)\n'
                      'print("> ", flush=True)\ntime.sleep(2)\n')
    binary.chmod(0o700)
    real_killpg = os.killpg
    signals = []
    def group(pgid, sig):
        signals.append(sig)
        if sig == 0:
            return  # Simulate an owned group surviving the entire cleanup budget.
        try:
            return real_killpg(pgid, sig)
        except ProcessLookupError:
            pass
    monkeypatch.setattr(os, 'killpg', group)
    with pytest.raises(CleanupIncompleteError, match='group_remaining'):
        read_usage(str(binary), ENV, .5)
    assert signal.SIGTERM in signals and signal.SIGKILL in signals


@pytest.mark.parametrize('prefix', ['', 'Type /help for commands\n', '\x1b]11;?\x07\x1b[32m'])
def test_real_116_login_menu_exits_without_input(tmp_path, prefix):
    from pathlib import Path
    from scripts.agy_usage import LoginRequiredError
    fixture = Path('tests/fixtures/agy/login-menu-116.txt').read_text()
    marker = tmp_path / 'input-received'
    binary = tmp_path / 'login-agy'
    binary.write_text('#!/usr/bin/python3\nimport sys\nfrom pathlib import Path\n'
                      'print(' + repr(prefix + fixture) + ', flush=True)\n'
                      'line = sys.stdin.readline()\n'
                      'Path(' + repr(str(marker)) + ').write_text(line)\n')
    binary.chmod(0o700)
    before = time.monotonic()
    with pytest.raises(LoginRequiredError, match='quota_login_required'):
        read_usage(str(binary), ENV, timeout=2)
    assert time.monotonic() - before < 1
    assert not marker.exists()


def test_runtime_login_discards_previous_measurement_and_backs_off():
    from scripts.agy_usage import LoginRequiredError
    calls = []
    def reader(*args):
        calls.append(1)
        if len(calls) == 1:
            return screen()
        raise LoginRequiredError('quota_login_required')
    async def run():
        collector = UsageCollector(reader)
        await collector.get(BINDING, '/agy', ENV, [])
        collector.next_attempt = 0
        result = await collector.get(BINDING, '/agy', ENV, [])
        assert result['usage_status'] == 'login_required'
        assert result['runtime_auth_status'] == 'login_required'
        assert result['groups'] == [] and result['used_percent'] is None
        assert result['usage_collected_at'] is None
        assert result == await collector.get(BINDING, '/agy', ENV, [])
        adapted = _antigravity_usage(result, ACCOUNT, 'needs_login', relay_available=True)
        assert adapted['usage_status'] == 'login_required'
        assert adapted['used_percent'] is None
        assert len(calls) == 2
    run_async(run())


def test_login_screen_cannot_be_parsed_as_available():
    from scripts.agy_usage import LoginRequiredError
    with pytest.raises(LoginRequiredError):
        parse_usage('Select login method:\n' + screen(), ACCOUNT, [])



def test_probe_serializes_only_anonymous_allowlisted_observations():
    from scripts.agy_runtime_probe import observation
    result = observation({'account': ACCOUNT, 'token': 'private-token',
                          'stderr': 'private-output', 'usage_status': 'unknown',
                          'groups': []})
    encoded = json.dumps(result)
    assert ACCOUNT not in encoded and 'private' not in encoded
    assert result['account_ref'] and result['usage_status'] == 'unknown'


@pytest.mark.parametrize('bad', [
    'weekly: unavailable', 'weekly: nonsense', 'weekly:',
    'weekly: -1% remaining', 'weekly: 101% remaining',
    'weekly: NaN% remaining', 'weekly: 50% remaining reset tomorrow',
    'five-hour: unavailable', 'five hour: 30% remaining',
    'weekly: 50% remaining\nweekly: 40% remaining',
    'Models: gemini-model-high',
])
def test_recognized_partial_text_never_ignored(bad):
    text = ('Account: a@example.com\nGemini\nModels: gemini-model-high\n'
            + bad + '\nfive-hour: 80% remaining\n')
    with pytest.raises(ValueError):
        parse_usage(text, ACCOUNT, [])


def test_bad_second_group_rejects_whole_observation_and_ansi_is_normalized():
    valid = ('Account: a@example.com\n\x1b[32mGemini\x1b[0m\n'
             'Models: gemini-model-high\nweekly: 70% remaining\n'
             '── decorative separator ──\nfive-hour: 80% remaining\n')
    assert len(parse_usage(valid, ACCOUNT, [])) == 1
    with pytest.raises(ValueError):
        parse_usage(valid + 'Claude and GPT\nModels: claude-model-high\nweekly: unavailable\n', ACCOUNT, [])


@pytest.mark.parametrize('reset', [False, 123, '', 'tomorrow', '2026-11-10T12:00:00'])
def test_parser_invalid_reset_is_not_available(reset):
    payload = json.loads(screen())
    payload['groups'][0]['windows'][0]['reset_at'] = reset
    with pytest.raises(ValueError):
        parse_usage(json.dumps(payload), ACCOUNT, [])


@pytest.mark.parametrize('bad', ['weekly: 50% remaining\x1b[', '\x1bXweekly: unavailable', 'weekly: 50% remaining\b'])
def test_unrecognized_terminal_controls_fail_closed(bad):
    with pytest.raises(ValueError):
        parse_usage('Account: a@example.com\nGemini\nModels: gemini-model-high\n' + bad + '\nfive-hour: 80% remaining', ACCOUNT, [])


@pytest.mark.parametrize('bad', ['Gemini: unavailable', 'Claude and GPT: unavailable'])
def test_malformed_recognized_group_cannot_be_ignored(bad):
    with pytest.raises(ValueError):
        parse_usage('Account: a@example.com\nGemini\nModels: gemini-model-high\nweekly: 60% remaining\n' + bad, ACCOUNT, [])


@pytest.mark.parametrize('bad', [
    screen() + '\nAccount: other@example.invalid',
    'Account: other@example.invalid\n' + screen(),
    screen().replace('"account":', '"account":"other@example.invalid","account":', 1),
    screen().replace('"groups":', '"groups":[],"groups":', 1),
    screen() + '\n' + screen('other@example.invalid'),
    screen().replace('"account":', '"acc\\u006funt":"A@example.com","account":', 1),
])
def test_r7_invalid_identity_clears_observation_through_relay_and_api(monkeypatch, bad):
    # Real quota HTTP handler and actual API adapter; external I/O is mocked.
    from scripts import agy_usage_server as server
    from tests.unit.test_llm_overview_antigravity import _run_overview, _agy
    from types import SimpleNamespace
    responses = iter([screen(), bad])
    collector = UsageCollector(lambda *args: next(responses), ttl=0, backoff=0)
    binding = dict(BINDING, kind='antigravity', account_verified=True)
    monkeypatch.setattr(server, 'collector', collector)
    monkeypatch.setattr(server, 'SECRET_PATH', SimpleNamespace(read_text=lambda: 'test-secret'))
    monkeypatch.setattr(server, '_antigravity_binding', lambda: dict(binding))
    monkeypatch.setattr(server, 'web', SimpleNamespace(json_response=lambda value: value))
    request = SimpleNamespace(headers={'X-Claude-Relay-Secret': 'test-secret'})
    async def run():
        good = await server.usage(request)
        assert good['runtime_auth_status'] == 'verified'
        failed = await server.usage(request)
        assert failed['groups'] == [] and failed['usage_collected_at'] is None
        assert failed['quota_account'] is None
        assert failed['runtime_auth_status'] == 'unknown'
        assert not failed['bound'] and not failed['account_verified']
        return failed
    failed = run_async(run())
    account = _agy(_run_overview(monkeypatch, {'bindings': [failed]}))[0]
    assert account['usage_status'] != 'available'
    assert not account['account_verified']
    assert account['groups'] == [] and account['usage_collected_at'] is None
    assert account['quota_account'] is None


def test_r7_same_account_parse_failure_preserves_stale_metadata():
    replies = iter([screen(), screen() + '\nweekly: unavailable'])
    async def run():
        collector = UsageCollector(lambda *args: next(replies), ttl=0, backoff=0)
        good = await collector.get(BINDING, '/agy', ENV, [])
        failed = await collector.get(BINDING, '/agy', ENV, [])
        assert failed['usage_collected_at'] == good['usage_collected_at']
        assert failed['quota_account'] == ACCOUNT
        assert failed['usage_source'] == good['usage_source']
        assert failed['runtime_auth_status'] == 'unknown'
        assert len(failed['groups']) == len(good['groups'])
        for group in failed['groups']:
            for window in group['windows']:
                assert window['stale']
                assert window['remaining_percent'] is window['used_percent'] is window['reset_at'] is None
    run_async(run())
