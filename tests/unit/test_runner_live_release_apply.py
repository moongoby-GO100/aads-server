"""runner-live release apply path: real Git, Bash, flock leases and fixture processes.

Only the service restart / MainPID / busy probe are injected (the controller
exposes them as overridable functions); no systemctl or production path is used.
Fixture processes are stopped through the host fixture's stop file.
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
SCRIPT = ROOT / 'scripts/runner_live_update.sh'
PREPARE = ROOT / 'scripts/runner_live_release_prepare.sh'
RESTART = ROOT / 'scripts/restart_local_runner.sh'
LIBRARY = ROOT / 'scripts/runner_busy_lib.sh'
spec = importlib.util.spec_from_file_location(
    'live_apply_protocol_fixture', ROOT / 'tests/unit/test_runner_maintenance_protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)

OLD_COMPANION = LIBRARY.read_bytes() + b'\n# fixture-old\n'
NEW_COMPANION = LIBRARY.read_bytes() + b'\n# fixture-new\n'
OTHER_COMPANION = LIBRARY.read_bytes() + b'\n# fixture-other\n'
# A real protocol daemon: it blocks in the admission flock until the lease is released.
RUNNER = '''#!/bin/bash
source "$FIXTURE_LIB"
declare -A _bg_jobs
runner_maintenance_enter "$FIXTURE_STATE" "$0" || exit 3
while [[ ! -f "$FIXTURE_STOP" ]]; do
    runner_maintenance_checkpoint_or_hold
    printf '.\\n' >> "$FIXTURE/claims.new"
    sleep .05
done
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(directory, *args):
    return subprocess.check_output(['git', '-C', str(directory), *args],
                                   text=True, stderr=subprocess.DEVNULL).strip()


DRIVER = r'''
source "$CONTROLLER"
STATE="$3"; LIVE="$7"
RUNNER_LIVE_COMPANION_PINS="$PINS"
RUNNER_LIVE_LEASE_WAIT=3
RUNNER_LIVE_ROLLBACK_LEASE_WAIT=5
RUNNER_LIVE_PID_WAIT=1
RUNNER_LIVE_RUNNING_WAIT=5
runner_live_busy_state() { cat "$FIXTURE/busy.state" 2>/dev/null || printf IDLE; }
runner_live_service_pid() { cat "$FIXTURE/svc.pid" 2>/dev/null; }
# Record the old daemon's claim count when the lease is first held and when the
# restart happens: a difference means the old daemon claimed work inside the lease.
eval "$(declare -f runner_live_apply_locked | sed '1s/runner_live_apply_locked/real_apply_locked/')"
runner_live_apply_locked() {
    wc -c < "$FIXTURE/claims" > "$FIXTURE/claims.at_lease" 2>/dev/null
    real_apply_locked "$@"
}
runner_live_restart_service() {
    # $1 = staged helper dir (the production body runs --lease-held from it)
    [[ -f "$1/restart_local_runner.sh" ]] || return 9
    printf 'x\n' >> "$FIXTURE/restarts"
    local mode
    mode=$(head -n1 "$FIXTURE/restart.modes" 2>/dev/null); mode=${mode:-ok}
    sed -i 1d "$FIXTURE/restart.modes" 2>/dev/null || true
    local script
    case "$mode" in
        ok) script="$LIVE/scripts/fixture-runner.sh" ;;
        wrongsource) script="$FIXTURE/other-runner.sh" ;;
        same_pid) return 0 ;;
        *) return 3 ;;
    esac
    wc -c < "$FIXTURE/claims" > "$FIXTURE/claims.at_restart" 2>/dev/null
    local old; old=$(cat "$FIXTURE/main.pid" 2>/dev/null)
    if [[ -n "$old" ]]; then pkill -P "$old" 2>/dev/null; kill "$old" 2>/dev/null; fi
    # The new daemon must not inherit the lease descriptors (systemd would not pass them).
    FIXTURE_LIB="$LIVE/scripts/runner_busy_lib.sh" FIXTURE_STATE="$STATE" FIXTURE_STOP="$FIXTURE/stop" \
        bash "$script" >/dev/null 2>&1 </dev/null 202>&- 203>&- &
    local pid=$! n
    for n in $(seq 1 200); do
        [[ -e /proc/$pid/fd/255 ]] && { [[ "$script" == *other-runner.sh || -e "$STATE/actors/$pid.json" ]] && break; }
        sleep .02
    done
    ln -sfn /proc/$pid "$FIXTURE/proc/$pid"
    printf '%s' "$pid" > "$FIXTURE/svc.pid"
    printf '%s' "$pid" > "$FIXTURE/main.pid"
}
eval "$(declare -f runner_live_wait_daemon_running | sed '1s/runner_live_wait_daemon_running/real_wait_running/')"
runner_live_wait_daemon_running() {
    local mode
    mode=$(head -n1 "$FIXTURE/running.modes" 2>/dev/null); mode=${mode:-ok}
    sed -i 1d "$FIXTURE/running.modes" 2>/dev/null || true
    [[ "$mode" == never ]] && return 1
    real_wait_running "$@"
}
if [[ -n "${CORRUPT_FIRST_INSTALL:-}" ]]; then
    eval "$(declare -f runner_live_install_atomic | sed '1s/runner_live_install_atomic/real_install_atomic/')"
    runner_live_install_atomic() {
        if [[ ! -f "$FIXTURE/corrupted" ]]; then
            touch "$FIXTURE/corrupted"
            printf 'tampered\n' > "$FIXTURE/tampered"
            real_install_atomic "$FIXTURE/tampered" "$2"
        else
            real_install_atomic "$@"
        fi
    }
fi
runner_live_update_gate "$@"
'''


class Release:
    def __init__(self, host):
        self.host = host
        base = host.path
        self.origin, self.seed, self.live = base / 'origin.git', base / 'seed', base / 'live'
        self.origin.mkdir()
        self.seed.mkdir()
        git(self.origin, 'init', '--bare', '-b', 'main')
        git(self.seed, 'init', '-b', 'main')
        git(self.seed, 'config', 'user.name', 'fixture')
        git(self.seed, 'config', 'user.email', 'fixture@example.invalid')
        git(self.seed, 'remote', 'add', 'origin', str(self.origin))
        self.old = self.commit('old', RUNNER, OLD_COMPANION)
        git(self.seed, 'push', 'origin', 'main')
        subprocess.run(['git', 'clone', '-q', '-b', 'main', str(self.origin), str(self.live)], check=True)
        git(self.live, 'config', 'user.name', 'fixture')
        git(self.live, 'config', 'user.email', 'fixture@example.invalid')
        self.release = self.commit('release', RUNNER + '# release bytes\n', NEW_COMPANION)
        git(self.seed, 'push', 'origin', 'main')
        git(self.seed, 'checkout', '-q', '-b', 'side', self.old)
        self.side = self.commit('side', RUNNER + '# side bytes\n', NEW_COMPANION)
        git(self.seed, 'push', 'origin', 'side')
        git(self.seed, 'checkout', '-q', 'main')
        git(self.live, 'fetch', '-q', 'origin', 'main', 'side')
        self.companion = base / 'runner_busy_lib.sh'
        self.companion.write_bytes(OLD_COMPANION)
        self.companion.chmod(0o644)
        self.manifest = base / 'release.json'
        (base / 'other-runner.sh').write_text(RUNNER + '# wrong source\n')
        (base / 'restarts').write_text('')
        self.pins = ' '.join(sha(b) for b in (OLD_COMPANION, NEW_COMPANION))
        self.actor = host.actor()
        (base / 'svc.pid').write_text(str(self.actor.pid))
        self.claims_ready()

    def claims_ready(self):
        protocol.wait_for(lambda: (self.host.path / 'claims').exists())

    def commit(self, subject, runner, companion):
        scripts = self.seed / 'scripts'
        scripts.mkdir(exist_ok=True)
        (scripts / "fixture-runner.sh").write_text(runner)
        (scripts / 'runner_busy_lib.sh').write_bytes(companion)
        (scripts / 'runner_live_update.sh').write_bytes(SCRIPT.read_bytes())
        (scripts / 'restart_local_runner.sh').write_bytes(RESTART.read_bytes())
        git(self.seed, 'add', '.')
        git(self.seed, 'commit', '-q', '-m', subject)
        return git(self.seed, 'rev-parse', 'HEAD')

    def write_manifest(self, mode=0o644, **override):
        doc = {'schema': 1, 'release_sha': self.release, 'expected_old_sha': self.old,
               'companion_sha256': sha(NEW_COMPANION), 'controller_sha256': sha(SCRIPT.read_bytes()),
               'created_at': '2026-10-14T16:53:00Z', 'approved_by': 'ceo-fixture'}
        doc.update(override)
        self.manifest.write_text(json.dumps(doc))
        self.manifest.chmod(mode)
        return doc

    def gate(self, extra_env=None):
        host = self.host
        env = {**host.env, 'CONTROLLER': str(SCRIPT), 'PINS': self.pins, **(extra_env or {})}
        args = [str(self.companion), str(self.manifest), str(host.state), 'fixture.service',
                str(host.proc), str(host.cg), str(self.live)]
        return subprocess.run(['bash', '-c', DRIVER, 'apply-fixture', *args],
                              env=env, text=True, capture_output=True, timeout=60)

    def restarts(self):
        return len((self.host.path / 'restarts').read_text().split())

    def set_modes(self, *modes):
        (self.host.path / 'restart.modes').write_text(''.join(m + '\n' for m in modes))

    def set_running_modes(self, *modes):
        (self.host.path / 'running.modes').write_text(''.join(m + '\n' for m in modes))

    def count(self, name):
        return int((self.host.path / name).read_text().split()[0])


@pytest.fixture
def host(tmp_path, monkeypatch):
    # The quiescence probe rejects any member whose cwd is under /tmp/aads-wt-*
    # (agent worktrees); fixture children must not inherit such a cwd.
    monkeypatch.chdir(tmp_path)
    fixture = protocol.Host(tmp_path)
    try:
        yield fixture
    finally:
        fixture.finish()


@pytest.fixture
def rel(host):
    return Release(host)


def head(rel):
    return git(rel.live, 'rev-parse', 'HEAD')


def assert_untouched(rel, restarts=0):
    assert head(rel) == rel.old
    assert rel.companion.read_bytes() == OLD_COMPANION
    assert rel.restarts() == restarts


def assert_defer(result, reason, extra=''):
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f'DEFER runner-live-update reason={reason}{extra}', result.stdout + result.stderr


def test_valid_manifest_applies_checkout_companion_and_new_pid(rel):
    rel.write_manifest()
    old_pid = rel.actor.pid
    result = rel.gate()
    assert result.returncode == 0, result.stdout + result.stderr
    new_pid = int((rel.host.path / 'svc.pid').read_text())
    assert new_pid != old_pid
    assert result.stdout.strip() == (
        f'APPLIED runner-live-update release={rel.release} old={rel.old} '
        f'prev_pid={old_pid} pid={new_pid} manifest=consumed')
    assert head(rel) == rel.release
    assert git(rel.live, 'status', '--porcelain') == ''
    assert rel.companion.read_bytes() == NEW_COMPANION
    assert rel.companion.stat().st_mode & 0o7777 == 0o644
    assert sorted(p.name for p in rel.host.path.glob('.runner_live.*')) == []
    assert rel.restarts() == 1
    assert rel.actor.poll() is not None  # the restart replaced the old daemon
    assert Path(f'/proc/{new_pid}/fd/255').resolve() == rel.live / 'scripts/fixture-runner.sh'
    actor = json.loads((rel.host.state / 'actors' / f'{new_pid}.json').read_text())
    assert actor['phase'] == 'RUNNING' and actor['source_sha256'] == sha(
        (rel.live / 'scripts/fixture-runner.sh').read_bytes())


def test_applied_manifest_is_consumed_and_cannot_reapply(rel):
    doc = rel.write_manifest()
    first = rel.gate()
    assert first.stdout.startswith('APPLIED runner-live-update'), first.stdout + first.stderr
    consumed = rel.host.path / f'release.applied.{rel.release}.json'
    assert not rel.manifest.exists()
    assert json.loads(consumed.read_text()) == doc
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_MISSING_AUTO_APPLY_DISABLED')
    # Even if the same manifest is put back, the old-SHA CAS refuses it.
    rel.manifest.write_text(consumed.read_text())
    rel.manifest.chmod(0o644)
    assert_defer(rel.gate(), 'OLD_SHA_MISMATCH', f' current={rel.release} expected={rel.old}')
    assert rel.restarts() == 1


def test_prepare_script_prints_manifest_to_stdout_and_installs_nothing(rel):
    before = (head(rel), git(rel.live, 'status', '--porcelain'), sorted(p.name for p in rel.host.path.iterdir()))
    result = subprocess.run(['bash', str(PREPARE), rel.release, '--approved-by', 'ceo fixture'],
                            env={**os.environ, 'AADS_RUNNER_LIVE_DIR': str(rel.live)},
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc == {'schema': 1, 'release_sha': rel.release, 'expected_old_sha': rel.old,
                   'companion_sha256': sha(NEW_COMPANION), 'controller_sha256': sha(SCRIPT.read_bytes()),
                   'created_at': doc['created_at'], 'approved_by': 'ceo fixture'}
    assert before == (head(rel), git(rel.live, 'status', '--porcelain'), sorted(p.name for p in rel.host.path.iterdir()))
    # The printed manifest is exactly what the controller accepts.
    rel.manifest.write_text(result.stdout)
    rel.manifest.chmod(0o644)
    assert rel.gate().stdout.startswith('APPLIED runner-live-update')


@pytest.mark.parametrize('arguments', [[], ['nothex'], [None], ['{old}']], ids=['none', 'bad', 'not-ancestor', 'already-there'])
def test_prepare_script_refuses_unsafe_requests(rel, arguments):
    args = [{'{old}': rel.old, None: rel.side}.get(a, a) for a in arguments]
    result = subprocess.run(['bash', str(PREPARE), *args], env={**os.environ, 'AADS_RUNNER_LIVE_DIR': str(rel.live)},
                            text=True, capture_output=True, timeout=30)
    assert result.returncode != 0
    assert result.stdout == ''


def test_old_sha_mismatch_defers_with_both_shas(rel):
    rel.write_manifest(expected_old_sha='f' * 40)
    assert_defer(rel.gate(), 'OLD_SHA_MISMATCH', f' current={rel.old} expected={"f" * 40}')
    assert_untouched(rel)
    assert rel.manifest.exists()


@pytest.mark.parametrize('dirt', ['untracked', 'modified'])
def test_dirty_tree_defers_and_is_not_cleaned(rel, dirt):
    rel.write_manifest()
    path = rel.live / ('evidence.txt' if dirt == 'untracked' else 'scripts/fixture-runner.sh')
    if dirt == 'untracked':
        path.write_text('worker evidence')
    else:
        path.write_text(path.read_text() + '# local edit\n')
    expected = path.read_text()
    assert_defer(rel.gate(), 'DIRTY_TREE')
    assert path.read_text() == expected
    assert_untouched(rel)


@pytest.mark.parametrize('state', ['BUSY', 'UNKNOWN', 'DB_BUSY_OR_UNKNOWN'])
def test_busy_runner_defers_without_touching_anything(rel, state):
    rel.write_manifest()
    (rel.host.path / 'busy.state').write_text(state)
    assert_defer(rel.gate(), 'BUSY', f' state={state}')
    assert_untouched(rel)
    assert rel.manifest.exists()


def test_lease_not_obtainable_defers_before_checkout(host):
    rel = Release(host)
    rel.actor.terminate()
    rel.actor.wait(timeout=5)
    (host.path / 'ready').unlink()
    (host.state / 'actors' / f'{rel.actor.pid}.json').unlink(missing_ok=True)
    rel.actor = host.actor(orphan=True)
    rel.write_manifest()
    result = rel.gate()
    assert_defer(result, 'LEASE_UNAVAILABLE')
    assert_untouched(rel)


def test_release_sha_not_ancestor_of_main_defers(rel):
    rel.write_manifest(release_sha=rel.side)
    assert_defer(rel.gate(), 'RELEASE_SHA_NOT_IN_MAIN', f' release={rel.side}')
    assert_untouched(rel)


def test_unknown_release_sha_defers(rel):
    rel.write_manifest(release_sha='a' * 40)
    assert_defer(rel.gate(), 'RELEASE_SHA_NOT_IN_MAIN', f' release={"a" * 40}')
    assert_untouched(rel)


def test_companion_sha_not_matching_release_blob_defers(rel):
    rel.pins += ' ' + sha(OTHER_COMPANION)
    rel.write_manifest(companion_sha256=sha(OTHER_COMPANION))
    assert_defer(rel.gate(), 'COMPANION_SHA_MISMATCH')
    assert_untouched(rel)


def test_unpinned_companion_hash_in_manifest_defers(rel):
    rel.write_manifest(companion_sha256=sha(OTHER_COMPANION))
    assert_defer(rel.gate(), 'COMPANION_PIN_MISMATCH')
    assert_untouched(rel)


def test_controller_sha_not_matching_running_controller_defers(rel):
    rel.write_manifest(controller_sha256='0' * 64)
    assert_defer(rel.gate(), 'CONTROLLER_SHA_MISMATCH')
    assert_untouched(rel)


def test_companion_corrupted_during_install_is_rolled_back(rel):
    rel.write_manifest()
    result = rel.gate({'CORRUPT_FIRST_INSTALL': '1'})
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.strip() == (
        f'ROLLED_BACK runner-live-update reason=COMPANION_SHA_MISMATCH release={rel.release} restored={rel.old}')
    assert_untouched(rel)
    assert (rel.host.path / f'release.rolledback.{rel.release}.json').exists()


@pytest.mark.parametrize('modes,reason,restarts', [
    (['same_pid'], 'PID_UNCHANGED', 1),
    (['fail'], 'RESTART_FAILED', 1),
    (['wrongsource', 'ok'], 'NEW_PID_SOURCE_MISMATCH', 2),
])
def test_new_pid_verification_failure_rolls_back_and_exits_nonzero(rel, modes, reason, restarts):
    rel.write_manifest()
    rel.set_modes(*modes)
    result = rel.gate()
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.strip() == (
        f'ROLLED_BACK runner-live-update reason={reason} release={rel.release} restored={rel.old}')
    assert_untouched(rel, restarts=restarts)
    assert git(rel.live, 'status', '--porcelain') == ''
    assert not rel.manifest.exists()
    assert (rel.host.path / f'release.rolledback.{rel.release}.json').exists()
    # A rolled-back manifest does not retry on the next timer tick.
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_MISSING_AUTO_APPLY_DISABLED')
    assert rel.restarts() == restarts
    if restarts == 2:
        restored = int((rel.host.path / 'svc.pid').read_text())
        assert Path(f'/proc/{restored}/fd/255').resolve() == rel.live / 'scripts/fixture-runner.sh'


def test_rollback_with_unverifiable_restart_reports_incomplete(rel):
    rel.write_manifest()
    rel.set_modes('wrongsource', 'wrongsource')
    result = rel.gate()
    assert result.returncode == 4, result.stdout + result.stderr
    assert result.stdout.startswith('ROLLBACK_INCOMPLETE runner-live-update reason=NEW_PID_SOURCE_MISMATCH')
    assert head(rel) == rel.old and rel.companion.read_bytes() == OLD_COMPANION


@pytest.mark.parametrize('change', [
    {'schema': 2}, {'schema': '1'}, {'schema': True}, {'release_sha': 'abc'}, {'release_sha': 'A' * 40},
    {'expected_old_sha': 'g' * 40}, {'companion_sha256': 'a' * 63}, {'controller_sha256': ''},
    {'created_at': 'yesterday'}, {'approved_by': 'x;rm -rf /'}, {'approved_by': ''},
    {'release_sha': 'REL', 'expected_old_sha': 'REL'}, {'extra': 'field'},
], ids=lambda c: next(iter(c)) + '=' + str(next(iter(c.values())))[:12])
def test_manifest_schema_violations_defer_invalid(rel, change):
    change = {k: (rel.release if v == 'REL' else v) for k, v in change.items()}
    rel.write_manifest(**change)
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_INVALID')
    assert_untouched(rel)


def test_manifest_missing_field_duplicate_key_and_modes_defer_invalid(rel):
    doc = rel.write_manifest()
    del doc['approved_by']
    rel.manifest.write_text(json.dumps(doc))
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_INVALID')
    rel.write_manifest()
    rel.manifest.write_text(rel.manifest.read_text().replace('"schema": 1', '"schema": 1, "schema": 1'))
    rel.manifest.chmod(0o644)
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_INVALID')
    rel.write_manifest(mode=0o664)
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_INVALID')
    rel.write_manifest(mode=0o755)
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_INVALID')
    rel.write_manifest()
    os.chown(rel.manifest, 65534, 65534)
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_INVALID')
    rel.manifest.unlink()
    real = rel.host.path / 'real.json'
    rel.write_manifest()
    rel.manifest.rename(real)
    rel.manifest.symlink_to(real)
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_INVALID')
    assert_untouched(rel)


def test_manifest_content_is_never_executed(rel):
    marker = rel.host.path / 'executed'
    rel.write_manifest(approved_by='$(touch x)')
    rel.manifest.write_text(rel.manifest.read_text().replace('$(touch x)', 'ok') + f'\ntouch {shlex.quote(str(marker))}\n')
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_INVALID')
    assert not marker.exists()


def test_old_daemon_claims_nothing_between_lease_and_applied_even_with_queued_work(rel):
    # The old fixture daemon claims on every pass through its main loop, like a
    # runner with a non-empty queue. Once the lease is held it must not claim again.
    rel.write_manifest()
    protocol.wait_for(lambda: (rel.host.path / 'claims').stat().st_size > 20)
    result = rel.gate()
    assert result.stdout.startswith('APPLIED runner-live-update'), result.stdout + result.stderr
    at_lease, at_restart = rel.count('claims.at_lease'), rel.count('claims.at_restart')
    assert at_lease > 0  # it really was claiming before the lease
    assert at_restart == at_lease
    # The old daemon is gone: its claim file never grows again, and the new daemon
    # (blocked in the admission flock until the lease was released) claims instead.
    final = (rel.host.path / 'claims').stat().st_size
    assert final == at_restart
    new_pid = int((rel.host.path / 'svc.pid').read_text())
    assert Path(f'/proc/{new_pid}').exists()
    protocol.wait_for(lambda: (rel.host.path / 'claims.new').exists())


def test_new_daemon_never_reaching_running_rolls_back_inside_one_lease(rel):
    rel.write_manifest()
    rel.set_running_modes('never')
    result = rel.gate()
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.strip() == (
        f'ROLLED_BACK runner-live-update reason=NEW_DAEMON_NOT_RUNNING release={rel.release} restored={rel.old}')
    assert_untouched(rel, restarts=2)
    assert git(rel.live, 'status', '--porcelain') == ''
    assert (rel.host.path / f'release.rolledback.{rel.release}.json').exists()
    restored = int((rel.host.path / 'svc.pid').read_text())
    assert Path(f'/proc/{restored}/fd/255').resolve() == rel.live / 'scripts/fixture-runner.sh'
    assert sha(Path(f'/proc/{restored}/fd/255').read_bytes()) == sha(RUNNER.encode())
    assert_defer(rel.gate(), 'RELEASE_MANIFEST_MISSING_AUTO_APPLY_DISABLED')


def test_rollback_restart_that_cannot_be_verified_is_incomplete(rel):
    rel.write_manifest()
    rel.set_running_modes('never', 'never')
    result = rel.gate()
    assert result.returncode == 4, result.stdout + result.stderr
    assert result.stdout.strip() == (
        f'ROLLBACK_INCOMPLETE runner-live-update reason=NEW_DAEMON_NOT_RUNNING release={rel.release} '
        f'expected_old={rel.old} restart=unverified')
    assert head(rel) == rel.old and rel.companion.read_bytes() == OLD_COMPANION


def test_restart_helpers_are_staged_from_the_release_not_the_live_tree(rel):
    # A rollback target without --lease-held must still restart through a script that has it.
    text = SCRIPT.read_text()
    assert '--lease-held' in text
    stage = text.split('runner_live_stage_restart_helpers() {', 1)[1].split('\n}\n', 1)[0]
    assert 'show "$release_sha:scripts/restart_local_runner.sh"' in stage
    assert 'companion.new' in stage and 'companion.prev' in stage


def seed_actor(root, pid, phase, source, digest=None, ticks=None):
    stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    (root / 'actors').mkdir(exist_ok=True)
    (root / 'actors' / f'{pid}.json').write_text(json.dumps({
        'protocol': 1, 'pid': pid, 'phase': phase, 'source': str(source),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'start_ticks': ticks or stat[19],
        'source_sha256': digest or sha(Path(source).read_bytes())}))


@pytest.mark.parametrize('case,expected', [
    ('running', 0), ('wait_admission', 1), ('hash_drift', 1), ('other_process', 1), ('no_record', 1)])
def test_running_check_requires_phase_identity_and_source_hash(tmp_path, case, expected):
    source = tmp_path / 'daemon.sh'
    source.write_text('#!/bin/bash\n')
    proc = subprocess.Popen(['sleep', '30'], cwd=tmp_path)
    try:
        root = tmp_path / 'state'
        root.mkdir()
        if case != 'no_record':
            seed_actor(root, proc.pid, 'WAIT_ADMISSION' if case == 'wait_admission' else 'RUNNING', source,
                       digest='0' * 64 if case == 'hash_drift' else None,
                       ticks='1' if case == 'other_process' else None)
        script = f'source {shlex.quote(str(SCRIPT))}\nrunner_live_wait_daemon_running {shlex.quote(str(root))} {proc.pid} 1\n'
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=20)
        assert result.returncode == expected, result.stdout + result.stderr
    finally:
        proc.terminate()
        proc.wait(timeout=5)


HELPER_LIB_OVERRIDES = r'''
eval "$(declare -f runner_maintenance_metadata | sed '1s/runner_maintenance_metadata/actual_metadata/')"
runner_maintenance_metadata() {
    case "$1" in
        preflight|quiescent) actual_metadata "$@" "$FIXTURE/proc" "$FIXTURE/cgroup" ;;
        restart-plan) printf '{}' ;;
        restart-verify) printf AWARE ;;
        *) actual_metadata "$@" ;;
    esac
}
runner_live_process_probe() { printf 'IDLE\n'; }
db_active_job_count() { touch "$FIXTURE/db-consulted"; printf 0; }
'''


def lease_held_restart(host, flag, timeout):
    helper = host.path / 'helper'
    helper.mkdir()
    state = str(host.state)
    (helper / 'restart_local_runner.sh').write_text(
        (ROOT / 'scripts/restart_local_runner.sh').read_text().replace('/run/aads-runner-maintenance', state))
    (helper / 'runner_busy_lib.sh').write_text(LIBRARY.read_text() + HELPER_LIB_OVERRIDES)
    host.actor()
    code = host.controller_library + f'''
cb() {{
    timeout {timeout} bash {shlex.quote(str(helper))}/restart_local_runner.sh --service fixture.service {flag}
    echo $? > "$FIXTURE/restart.rc"
}}
runner_maintenance_transaction fixture.service 3 {shlex.quote(state)} cb
'''
    result = subprocess.run(['bash', '-c', code], env=host.env, capture_output=True, text=True, timeout=30)
    return result, int((host.path / 'restart.rc').read_text())


def test_lease_held_restart_finishes_without_deadlock_and_skips_db_busy(host):
    result, rc = lease_held_restart(host, '--lease-held', 15)
    assert rc == 0, result.stdout + result.stderr
    assert (host.path / 'systemctl-mutations').read_text().splitlines().count('restart fixture.service') == 1
    assert not (host.path / 'db-consulted').exists()


def test_restart_without_lease_held_flag_deadlocks_on_its_own_transaction(host):
    # Why the option exists: a second EX request on the same lock from another fd never succeeds.
    result, rc = lease_held_restart(host, '', 4)
    assert rc == 124, result.stdout + result.stderr
    mutations = (host.path / 'systemctl-mutations')
    assert 'restart fixture.service' not in (mutations.read_text() if mutations.exists() else '')
