"""runner-live controller/companion installer: real Git, Bash, flock leases and fixture daemons.

Every path is a temporary fixture directory handed to installer_run() as an
argument; the production install paths (/usr/local/sbin, /usr/local/lib/aads-runner,
/etc/aads) are never touched. Only the atomic-install step is wrapped to inject a
rename failure (a function redefinition, not an environment variable).
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/install_runner_live_controller.sh'
LIBRARY = ROOT / 'scripts/runner_busy_lib.sh'
spec = importlib.util.spec_from_file_location(
    'install_protocol_fixture', ROOT / 'tests/unit/test_runner_maintenance_protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)

OLD_COMPANION = LIBRARY.read_bytes() + b'\n# installed-old\n'
NEW_COMPANION = LIBRARY.read_bytes() + b'\n# release-new\n'
OTHER_COMPANION = LIBRARY.read_bytes() + b'\n# release-other\n'
OLD_CONTROLLER = b'#!/usr/bin/env bash\n# installed controller (old)\n'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def controller_blob(*pinned, tag):
    pins = ' '.join(sha(p) for p in pinned)
    return f"#!/usr/bin/env bash\n# {tag}\nRUNNER_LIVE_COMPANION_PINS='{pins}'\n".encode()


NEW_CONTROLLER = controller_blob(OLD_COMPANION, NEW_COMPANION, tag='release controller')
BADPIN_CONTROLLER = controller_blob(OLD_COMPANION, OTHER_COMPANION, tag='badpin controller')


def git(directory, *args):
    return subprocess.check_output(['git', '-C', str(directory), *args],
                                   text=True, stderr=subprocess.DEVNULL).strip()


DRIVER = r'''
source "$INSTALLER"
INSTALLER_LEASE_WAIT=3
if [[ -n "${FAIL_DEST:-}" ]]; then
    eval "$(declare -f installer_install_atomic | sed '1s/installer_install_atomic/real_install_atomic/')"
    installer_install_atomic() {
        [[ "$2" == "$FAIL_DEST" ]] && return 3
        real_install_atomic "$@"
    }
fi
installer_run "$@"
'''


class Fixture:
    def __init__(self, host, orphan=False):
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
        self.old = self.commit('old', OLD_CONTROLLER, OLD_COMPANION)
        git(self.seed, 'push', 'origin', 'main')
        subprocess.run(['git', 'clone', '-q', '-b', 'main', str(self.origin), str(self.live)], check=True)
        self.release = self.commit('release', NEW_CONTROLLER, NEW_COMPANION)
        self.badpin = self.commit('badpin', BADPIN_CONTROLLER, NEW_COMPANION)
        git(self.seed, 'push', 'origin', 'main')
        git(self.seed, 'checkout', '-q', '-b', 'side', self.old)
        self.side = self.commit('side', NEW_CONTROLLER, NEW_COMPANION)
        git(self.seed, 'push', 'origin', 'side')
        git(self.live, 'fetch', '-q', 'origin', 'main', 'side')

        sbin, lib = base / 'inst/sbin', base / 'inst/lib'
        sbin.mkdir(parents=True)
        lib.mkdir(parents=True)
        self.controller, self.companion = sbin / 'aads-runner-live-update', lib / 'runner_busy_lib.sh'
        self.controller.write_bytes(OLD_CONTROLLER)
        self.controller.chmod(0o755)
        self.companion.write_bytes(OLD_COMPANION)
        self.companion.chmod(0o644)
        self.actor = host.actor(orphan=orphan)

    def commit(self, subject, controller, companion):
        scripts = self.seed / 'scripts'
        scripts.mkdir(exist_ok=True)
        (scripts / 'runner_live_update.sh').write_bytes(controller)
        (scripts / 'runner_busy_lib.sh').write_bytes(companion)
        git(self.seed, 'add', '.')
        git(self.seed, 'commit', '-q', '-m', subject)
        return git(self.seed, 'rev-parse', 'HEAD')

    def run(self, *flags, extra_env=None):
        host = self.host
        env = {**host.env, 'INSTALLER': str(SCRIPT), 'TMPDIR': str(host.path), **(extra_env or {})}
        args = [str(self.controller), str(self.companion), str(host.state), 'fixture.service',
                str(host.proc), str(host.cg), str(self.live), *flags]
        return subprocess.run(['bash', '-c', DRIVER, 'install-fixture', *args],
                              env=env, text=True, capture_output=True, timeout=60)

    def flags(self, release=None, controller=None, companion=None, *more):
        return ['--release-sha', release or self.release,
                '--expect-controller-sha256', controller or sha(OLD_CONTROLLER),
                '--expect-companion-sha256', companion or sha(OLD_COMPANION), *more]

    def listing(self):
        return sorted(str(p.relative_to(self.host.path / 'inst')) for p in (self.host.path / 'inst').rglob('*'))


@pytest.fixture
def host(tmp_path, monkeypatch):
    # The quiescence probe rejects members whose cwd is under /tmp/aads-wt-* (agent worktrees).
    monkeypatch.chdir(tmp_path)
    fixture = protocol.Host(tmp_path)
    try:
        yield fixture
    finally:
        fixture.finish()


@pytest.fixture
def fx(host):
    return Fixture(host)


def assert_untouched(fx):
    assert fx.controller.read_bytes() == OLD_CONTROLLER
    assert fx.companion.read_bytes() == OLD_COMPANION
    assert fx.listing() == ['lib', 'lib/runner_busy_lib.sh', 'sbin', 'sbin/aads-runner-live-update']


def test_installs_companion_then_controller_with_backups(fx):
    result = fx.run(*fx.flags())
    assert result.returncode == 0, result.stdout + result.stderr
    old_c, new_c, old_l, new_l = sha(OLD_CONTROLLER)[:16], sha(NEW_CONTROLLER)[:16], sha(OLD_COMPANION)[:16], sha(NEW_COMPANION)[:16]
    assert result.stdout.strip() == (
        f'INSTALLED runner-live-install release={fx.release} controller_old={old_c} controller_new={new_c} '
        f'companion_old={old_l} companion_new={new_l} '
        f'backup_controller={fx.controller}.prev.{old_c} backup_companion={fx.companion}.prev.{old_l}')
    assert fx.controller.read_bytes() == NEW_CONTROLLER
    assert fx.companion.read_bytes() == NEW_COMPANION
    assert fx.controller.stat().st_mode & 0o7777 == 0o755
    assert fx.companion.stat().st_mode & 0o7777 == 0o644
    assert (fx.controller.stat().st_uid, fx.controller.stat().st_gid) == (0, 0)
    assert (fx.companion.stat().st_uid, fx.companion.stat().st_gid) == (0, 0)
    assert Path(f'{fx.controller}.prev.{old_c}').read_bytes() == OLD_CONTROLLER
    assert Path(f'{fx.companion}.prev.{old_l}').read_bytes() == OLD_COMPANION
    assert fx.listing() == sorted([
        'lib', 'lib/runner_busy_lib.sh', f'lib/runner_busy_lib.sh.prev.{old_l}',
        'sbin', 'sbin/aads-runner-live-update', f'sbin/aads-runner-live-update.prev.{old_c}'])
    assert not list(fx.host.path.glob('runner-live-install.*'))


def test_installed_checksum_mismatch_defers_and_writes_nothing(fx):
    wrong = 'f' * 64
    result = fx.run(*fx.flags(controller=wrong))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (
        f'DEFER runner-live-install reason=INSTALLED_CHECKSUM_MISMATCH controller_current={sha(OLD_CONTROLLER)} '
        f'controller_expected={wrong} companion_current={sha(OLD_COMPANION)} companion_expected={sha(OLD_COMPANION)}')
    assert_untouched(fx)
    result = fx.run(*fx.flags(companion=wrong))
    assert 'reason=INSTALLED_CHECKSUM_MISMATCH' in result.stdout and f'companion_expected={wrong}' in result.stdout
    assert_untouched(fx)


def test_missing_installed_file_defers(fx):
    fx.controller.unlink()
    result = fx.run(*fx.flags())
    assert 'reason=INSTALLED_CHECKSUM_MISMATCH controller_current=missing' in result.stdout
    assert fx.companion.read_bytes() == OLD_COMPANION


def test_release_sha_not_ancestor_of_main_defers(fx):
    result = fx.run(*fx.flags(release=fx.side))
    assert result.stdout.strip() == f'DEFER runner-live-install reason=RELEASE_SHA_NOT_IN_MAIN release={fx.side}'
    assert_untouched(fx)
    result = fx.run(*fx.flags(release='a' * 40))
    assert result.stdout.strip() == f'DEFER runner-live-install reason=RELEASE_SHA_NOT_IN_MAIN release={"a" * 40}'
    assert_untouched(fx)


def test_companion_pin_missing_from_new_controller_defers(fx):
    result = fx.run(*fx.flags(release=fx.badpin))
    assert result.stdout.strip() == (
        f'DEFER runner-live-install reason=COMPANION_PIN_MISMATCH companion_new={sha(NEW_COMPANION)}')
    assert_untouched(fx)


def test_controller_rename_failure_restores_companion(fx):
    result = fx.run(*fx.flags(), extra_env={'FAIL_DEST': str(fx.controller)})
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.strip() == (
        f'ROLLED_BACK runner-live-install reason=CONTROLLER_RENAME_FAILED release={fx.release} '
        f'controller_restored={sha(OLD_CONTROLLER)[:16]} companion_restored={sha(OLD_COMPANION)[:16]}')
    assert fx.controller.read_bytes() == OLD_CONTROLLER
    assert fx.companion.read_bytes() == OLD_COMPANION
    assert fx.companion.stat().st_mode & 0o7777 == 0o644
    assert Path(f'{fx.companion}.prev.{sha(OLD_COMPANION)[:16]}').read_bytes() == OLD_COMPANION
    assert not [n for n in fx.listing() if '.runner_live_install.' in n]


def test_dry_run_writes_nothing_and_prints_commands(fx):
    before = fx.listing()
    result = fx.run(*fx.flags(), '--dry-run')
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.strip().splitlines()
    old_c, old_l = sha(OLD_CONTROLLER), sha(OLD_COMPANION)
    assert lines[0] == (
        f'DRYRUN runner-live-install release={fx.release} controller={fx.controller} controller_current={old_c[:16]} '
        f'controller_new={sha(NEW_CONTROLLER)[:16]} companion={fx.companion} companion_current={old_l[:16]} '
        f'companion_new={sha(NEW_COMPANION)[:16]} pin=ok preflight=AWARE writes=none')
    assert lines[1] == (
        f'INSTALL_CMD: bash {SCRIPT} --release-sha {fx.release} '
        f'--expect-controller-sha256 {old_c} --expect-companion-sha256 {old_l}')
    assert lines[2].startswith('ROLLBACK_CMD: install -o root -g root -m 0644 ') and f'.prev.{old_l[:16]}' in lines[2]
    assert lines[3].startswith('ROLLBACK_CMD: install -o root -g root -m 0755 ') and f'.prev.{old_c[:16]}' in lines[3]
    assert len(lines) == 4
    assert fx.listing() == before
    assert_untouched(fx)


def test_dry_run_still_defers_on_mismatch(fx):
    result = fx.run(*fx.flags(controller='f' * 64), '--dry-run')
    assert result.stdout.startswith('DEFER runner-live-install reason=INSTALLED_CHECKSUM_MISMATCH')
    assert 'DRYRUN' not in result.stdout and 'INSTALL_CMD' not in result.stdout


def test_lease_unavailable_defers_without_writing(host):
    fx = Fixture(host, orphan=True)
    result = fx.run(*fx.flags())
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'DEFER runner-live-install reason=LEASE_UNAVAILABLE'
    assert_untouched(fx)


@pytest.mark.parametrize('flags', [
    [],
    ['--release-sha', 'nothex', '--expect-controller-sha256', 'a' * 64, '--expect-companion-sha256', 'b' * 64],
    ['--release-sha', 'a' * 40, '--expect-controller-sha256', 'a' * 64],
    ['--release-sha', 'a' * 40, '--release-sha', 'b' * 40, '--expect-controller-sha256', 'a' * 64,
     '--expect-companion-sha256', 'b' * 64],
    ['--release-sha', 'a' * 40, '--expect-controller-sha256', 'a' * 64, '--expect-companion-sha256', 'b' * 64, '--force'],
], ids=['none', 'bad-sha', 'missing-companion', 'duplicate', 'unknown-flag'])
def test_bad_arguments_exit_2_and_write_nothing(fx, flags):
    result = fx.run(*flags)
    assert result.returncode == 2
    assert result.stdout == ''
    assert_untouched(fx)


def test_production_paths_are_fixed_and_environment_is_not_read():
    text = SCRIPT.read_text()
    assert 'os.environ' not in text and 'getenv' not in text
    main = text.split('installer_main() {', 1)[1].split('\n}\n', 1)[0]
    for fixed in ('/usr/local/sbin/aads-runner-live-update', '/usr/local/lib/aads-runner/runner_busy_lib.sh',
                  '/run/aads-runner-maintenance', 'aads-pipeline-runner.service', '/root/aads/runner-live'):
        assert fixed in main
    assert '${' not in main.replace('"$@"', '')
    unsupported = subprocess.run(['bash', str(SCRIPT), '--bogus'], capture_output=True, text=True, timeout=20,
                                 env={**os.environ, 'HOME': '/nonexistent'})
    assert unsupported.returncode == 2 and unsupported.stdout == ''
