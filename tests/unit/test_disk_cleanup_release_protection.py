"""Execute the cleanup image selector with fake Docker; never touch Docker/DB."""
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "scripts/disk_cleanup_v2.sh").read_text()


def test_cleanup_uses_deployment_lock_before_any_cleanup():
    guard = (ROOT / "scripts/disk_guard.sh").read_text()
    assert '/tmp/aads-deploy.flock' in guard
    assert 'exec 8>"${AADS_DEPLOY_FLOCKFILE:-/tmp/aads-deploy.flock}"' in SOURCE
    assert SOURCE.index('flock -n 8') < SOURCE.index('mkdir -p')


def _run_selector(tmp_path, *, ledger_fails=False):
    selector = SOURCE.split('prune_project_images() {', 1)[1].split('\ndocker image prune', 1)[0]
    harness = r'''
docker() {
    case "$1 $2" in
        "exec aads-postgres")
            if [[ "${LEDGER_FAIL:-}" == 1 ]]; then return 1; fi
            printf 'pending123\nrollback456\n' ;;
        "image ls")
            printf '%s\n' aads-server-deps:deps123 aads-server:pending123 aads-dashboard:rollback456 aads-server:active789 aads-server:obsolete ;;
        "image rm") printf 'removed:%s\n' "$3" ;;
        *) return 99 ;;
    esac
}
is_active_image() { [[ "$1" == aads-server:active789 ]]; }
'''
    result = subprocess.run(
        ['bash', '-c', harness + '\nprune_project_images() {' + selector + '\nprune_project_images\n'],
        env={**os.environ, 'LOG': str(tmp_path / 'cleanup.log'), 'LEDGER_FAIL': '1' if ledger_fails else '0'},
        capture_output=True, text=True, check=True,
    )
    return result.stdout + (tmp_path / 'cleanup.log').read_text()


def test_selector_preserves_dependencies_pending_rollback_and_live_images(tmp_path):
    output = _run_selector(tmp_path)
    assert 'removed:aads-server:obsolete' in output
    for tag in ('aads-server-deps:deps123', 'aads-server:pending123', 'aads-dashboard:rollback456', 'aads-server:active789'):
        assert f'removed:{tag}' not in output


def test_selector_fails_closed_without_release_ledger(tmp_path):
    output = _run_selector(tmp_path, ledger_fails=True)
    assert 'tagged-image cleanup deferred' in output
    assert 'removed:' not in output
