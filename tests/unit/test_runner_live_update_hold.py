"""HOLD controller regressions with real Git, Bash, flock and fixture processes.

There is intentionally no positive apply test: this release has no apply path.
Only fixture subprocesses are stopped; production services are never mutated.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(os.environ.get('AADS_RUNNER_LIVE_CONTROLLER_UNDER_TEST',
                            str(ROOT / 'scripts/runner_live_update.sh')))
LIBRARY = ROOT / 'scripts/runner_busy_lib.sh'
PINNED = 'ff637e3f7f66b968338c4b439cd1e144d57326cafffa4f5ccf96d76e51be5026'
spec = importlib.util.spec_from_file_location(
    'live_hold_real_protocol_fixture', ROOT / 'tests/unit/test_runner_maintenance_protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


@pytest.fixture
def host(tmp_path):
    fixture = protocol.Host(tmp_path)
    try:
        yield fixture
    finally:
        fixture.finish()


def aware_actor(host, worker=False, orphan=False):
    # A standing enrolled actor needs no checkpoint for this read-only HOLD
    # probe: the controller never takes admission EX. Use Bash's timed read for
    # its work loop so cgroup snapshots do not race short-lived polling children.
    # Enrollment, open source FD255, real SH lock and worker inheritance are real.
    fifo = host.path / 'fixture-wait.fifo'
    os.mkfifo(fifo)
    path = host.path / 'actor.sh'
    code = host.library + '''
set -e
exec 199<>"$FIXTURE/fixture-wait.fifo"
runner_maintenance_enter "$FIXTURE/state" "$0"
printf '%s' "$$" > "$FIXTURE/main.pid"
'''
    if worker or orphan:
        code += '''(
 printf ready > "$FIXTURE/worker-ready"
 while [[ ! -f "$FIXTURE/worker-done" ]]; do read -r -t .02 -u 199 value || :; done
) &
printf '%s' "$!" > "$FIXTURE/worker.pid"
'''
    code += '''printf ready > "$FIXTURE/ready"
while [[ ! -f "$FIXTURE/stop" ]]; do
 printf '.\\n' >> "$FIXTURE/claims"
 read -r -t .02 -u 199 value || :
done
'''
    path.write_text(code)
    log = (host.path / 'actor.sh.log').open('w')
    actor = subprocess.Popen(['bash', str(path)], env=host.env, stdout=log, stderr=log)
    host.pids.append((actor, log))
    protocol.wait_for(lambda: (host.path / 'ready').exists())
    if worker or orphan:
        protocol.wait_for(lambda: (host.path / 'worker-ready').exists())
    return actor


def gate(host, companion=None, manifest=None):
    args = [str(companion or host.path / 'runner_busy_lib.sh'),
            str(manifest or host.path / 'release.json'), str(host.state),
            'fixture.service', str(host.proc), str(host.cg)]
    code = f'source {shlex.quote(str(SCRIPT))}\nrunner_live_update_gate "$@"\n'
    return subprocess.run(['bash', '-c', code, 'hold-fixture', *args],
                          env=host.env, text=True, capture_output=True, timeout=20)


def assert_deferred(result, reason):
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'DEFER runner-live-update reason=' + reason


@pytest.mark.parametrize('change', ['missing', 'bytes', 'symlink', 'writable', 'unowned', 'parent_symlink'])
def test_untrusted_companion_is_never_executed(host, change):
    companion = host.path / 'runner_busy_lib.sh'
    marker = host.path / 'untrusted-executed'
    if change == 'missing':
        companion.unlink()
    elif change == 'bytes':
        companion.write_text(f'touch {shlex.quote(str(marker))}\n')
    elif change == 'symlink':
        companion.unlink()
        companion.symlink_to(LIBRARY)
    elif change == 'writable':
        companion.chmod(0o666)
    elif change == 'unowned':
        os.chown(companion, 65534, 65534)
    elif change == 'parent_symlink':
        link = host.path / 'alias'
        link.symlink_to(host.path, target_is_directory=True)
        companion = link / companion.name
    assert_deferred(gate(host, companion), 'COMPANION_MISSING_OR_UNTRUSTED')
    assert not marker.exists()
    assert not (host.path / 'systemctl-mutations').exists()


def test_legacy_without_protocol_metadata_defers(host):
    assert hashlib.sha256(LIBRARY.read_bytes()).hexdigest() == PINNED
    assert_deferred(gate(host), 'BOOTSTRAP_REQUIRED')
    assert not host.state.exists(), 'diagnostic must not initialize enrollment'
    assert not (host.path / 'systemctl-mutations').exists()


def test_unknown_os_probe_defers_without_any_transition(host):
    aware_actor(host)
    # The actual metadata implementation receives undecodable OS output and
    # returns UNKNOWN. No implementation function or result is mocked.
    (host.bin / 'systemctl').write_text('#!/usr/bin/env python3\nimport os\nos.write(1, b"\\xff")\n')
    assert_deferred(gate(host), 'PREFLIGHT_UNKNOWN')
    assert not (host.path / 'systemctl-mutations').exists()


def test_aware_without_release_config_still_defers_and_keeps_worker(host):
    actor = aware_actor(host, worker=True)
    worker = int((host.path / 'worker.pid').read_text())
    descriptor = Path(f'/proc/{actor.pid}/fd/200').stat()
    assert_deferred(gate(host), 'RELEASE_MANIFEST_MISSING_AUTO_APPLY_DISABLED')
    assert actor.poll() is None
    assert Path(f'/proc/{worker}').exists()
    assert Path(f'/proc/{actor.pid}/fd/200').stat().st_ino == descriptor.st_ino
    assert not (host.path / 'systemctl-mutations').exists()
    assert not (host.path / 'applied').exists()


@pytest.mark.parametrize('manifest_kind', ['complete', 'invalid_json', 'shell_payload', 'symlink'])
def test_even_complete_manifest_cannot_enable_auto_apply(host, manifest_kind):
    actor = aware_actor(host, orphan=True)
    manifest = host.path / 'release.json'
    marker = host.path / 'manifest-executed'
    if manifest_kind == 'complete':
        manifest.write_text(json.dumps({'commit': '0' * 40, 'runtime_protocol': 1,
                                        'runner_sha256': hashlib.sha256((host.path / 'actor.sh').read_bytes()).hexdigest(),
                                        'helper_sha256': PINNED, 'approved': True}))
    elif manifest_kind == 'invalid_json':
        manifest.write_text('{ not JSON')
    elif manifest_kind == 'shell_payload':
        manifest.write_text(f'touch {shlex.quote(str(marker))}\n')
    else:
        manifest.symlink_to(host.path / 'actor.sh')
    protocol.wait_for(lambda: (host.path / 'claims').exists())
    before = (host.path / 'claims').read_bytes()
    assert_deferred(gate(host), 'RELEASE_MANIFEST_NOT_ACCEPTED_AUTO_APPLY_DISABLED')
    assert actor.poll() is None
    protocol.wait_for(lambda: (host.path / 'claims').read_bytes() != before)
    assert not marker.exists()
    assert not (host.path / 'applied').exists()
    assert not (host.path / 'systemctl-mutations').exists()


def test_companion_replaced_after_verified_read_is_not_executed(host):
    companion = host.path / 'runner_busy_lib.sh'
    marker = host.path / 'replaced-executed'
    code = f'''source {shlex.quote(str(SCRIPT))}
eval "$(declare -f runner_live_verified_companion | sed '1s/runner_live_verified_companion/read_verified_companion/')"
runner_live_verified_companion() {{
    local verified
    verified=$(read_verified_companion "$1") || return
    printf 'touch %s\\n' {shlex.quote(str(marker))} > "$1.replacement"
    mv "$1.replacement" "$1"
    printf '%s' "$verified"
}}
runner_live_update_gate "$@"
'''
    result = subprocess.run(['bash', '-c', code, 'hold-fixture', str(companion),
                             str(host.path / 'release.json'), str(host.state),
                             'fixture.service', str(host.proc), str(host.cg)],
                            env=host.env, text=True, capture_output=True, timeout=20)
    assert_deferred(result, 'BOOTSTRAP_REQUIRED')
    assert not marker.exists()


def git(directory, *args):
    return subprocess.check_output(['git', '-C', str(directory), *args],
                                   text=True, stderr=subprocess.DEVNULL).strip()


@pytest.mark.parametrize('arguments', [[], ['--apply']], ids=['timer-default', 'apply-cannot-enable'])
def test_entrypoint_cannot_checkout_advanced_origin_even_with_apply_argument(tmp_path, arguments):
    origin, seed, live = (tmp_path / name for name in ('origin.git', 'seed', 'live'))
    origin.mkdir(); seed.mkdir()
    git(origin, 'init', '--bare')
    git(seed, 'init', '-b', 'main')
    git(seed, 'config', 'user.name', 'fixture')
    git(seed, 'config', 'user.email', 'fixture@example.invalid')
    source = seed / 'scripts/pipeline-runner.sh'
    source.parent.mkdir()
    source.write_text('#!/bin/bash\nwhile [[ ! -f "$FIXTURE_STOP" ]]; do sleep .02; done\n')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'old')
    git(seed, 'remote', 'add', 'origin', str(origin))
    git(seed, 'push', 'origin', 'main')
    subprocess.run(['git', 'clone', '-q', '-b', 'main', str(origin), str(live)], check=True)
    source.write_text(source.read_text() + '# newer remote bytes\n')
    git(seed, 'commit', '-am', 'new')
    git(seed, 'push', 'origin', 'main')
    running_source = live / 'scripts/pipeline-runner.sh'
    (live / 'worker-output.txt').write_text('untracked worker evidence')
    stop = tmp_path / 'stop'
    env = {**os.environ, 'FIXTURE_STOP': str(stop), 'AADS_RUNNER_LIVE_DIR': str(live)}
    actor = subprocess.Popen(['bash', str(running_source)], env=env)
    try:
        protocol.wait_for(lambda: Path(f'/proc/{actor.pid}/fd/255').exists())
        before = (git(live, 'rev-parse', 'HEAD'), git(live, 'rev-parse', 'origin/main'),
                  git(live, 'status', '--porcelain'), running_source.read_bytes(), running_source.stat().st_ino)
        result = subprocess.run(['bash', str(SCRIPT), *arguments], env=env,
                                text=True, capture_output=True, timeout=20)
        after = (git(live, 'rev-parse', 'HEAD'), git(live, 'rev-parse', 'origin/main'),
                 git(live, 'status', '--porcelain'), running_source.read_bytes(), running_source.stat().st_ino)
        assert before == after
        if arguments:
            assert_deferred(result, 'UNSUPPORTED_ARGUMENTS_AUTO_APPLY_DISABLED')
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            assert result.stdout.strip() in {
                'DEFER runner-live-update reason=' + reason for reason in (
                    'COMPANION_MISSING_OR_UNTRUSTED', 'BOOTSTRAP_REQUIRED', 'PREFLIGHT_UNKNOWN',
                    'RELEASE_MANIFEST_MISSING_AUTO_APPLY_DISABLED',
                    'RELEASE_MANIFEST_NOT_ACCEPTED_AUTO_APPLY_DISABLED')}
        assert Path(f'/proc/{actor.pid}/fd/255').stat().st_ino == before[-1]
        assert actor.poll() is None
        assert (live / 'worker-output.txt').read_text() == 'untracked worker evidence'
    finally:
        stop.touch()
        actor.wait(timeout=5)


def test_default_entrypoint_pins_paths_and_keeps_aware_actor_running(host):
    actor = aware_actor(host, worker=True)
    # Translate only fixed OS paths into fixture paths. Actual main, verification
    # and metadata functions run; the fixture owns the real actor/flock state.
    code = f'''source {shlex.quote(str(SCRIPT))}
eval "$(declare -f runner_live_update_gate | sed '1s/runner_live_update_gate/actual_gate/')"
runner_live_update_gate() {{
    [[ "$1" == /usr/local/lib/aads-runner/runner_busy_lib.sh ]] || return 91
    [[ "$2" == /etc/aads/runner-live-release.json ]] || return 92
    [[ "$3" == /run/aads-runner-maintenance ]] || return 93
    [[ "$4" == aads-pipeline-runner.service ]] || return 94
    export PATH="$FIXTURE/bin:$PATH"
    actual_gate "$FIXTURE/runner_busy_lib.sh" "$FIXTURE/release.json" "$FIXTURE/state" fixture.service "$FIXTURE/proc" "$FIXTURE/cgroup"
}}
runner_live_update_main
'''
    env = {**host.env, 'AADS_RUNNER_LIVE_DIR': '/wrong/checkout',
           'AADS_RUNNER_LIVE_COMPANION': '/wrong/helper',
           'AADS_RUNNER_LIVE_RELEASE': '/wrong/manifest'}
    result = subprocess.run(['bash', '-c', code], env=env, text=True,
                            capture_output=True, timeout=20)
    assert_deferred(result, 'RELEASE_MANIFEST_MISSING_AUTO_APPLY_DISABLED')
    assert actor.poll() is None
    assert not (host.path / 'systemctl-mutations').exists()


def test_entrypoint_paths_are_fixed_and_positive_apply_is_absent():
    text = SCRIPT.read_text()
    main = text.split('runner_live_update_main() {', 1)[1].split('\n}', 1)[0]
    assert '/usr/local/lib/aads-runner/runner_busy_lib.sh' in main
    assert '/etc/aads/runner-live-release.json' in main
    assert '/run/aads-runner-maintenance' in main
    assert 'AADS_RUNNER_LIVE_DIR' not in main
    assert 'runner_maintenance_transaction' not in text
    assert 'systemctl restart' not in text
    assert 'git checkout' not in text
    assert 'git fetch' not in text
