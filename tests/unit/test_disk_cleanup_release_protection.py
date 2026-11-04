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


def _run_stale_test_reaper(tmp_path):
    # 2026-11-04: 멈춘 pytest 컨테이너가 옛 릴리스 이미지를 "active" 로 붙잡아 회수 0% 가 반복됐다.
    block = "STALE_TEST_SECONDS=" + SOURCE.split("STALE_TEST_SECONDS=", 1)[1].split("\nACTIVE_IMAGE_IDS=", 1)[0]
    fake = tmp_path / "docker"
    fake.write_text(r'''#!/bin/bash
case "$1" in
  ps) printf 'old_ut\nold_plain\nfresh_ut\napi\n' ;;
  inspect)
    case "$4" in
      old_ut) echo "/old_ut|2026-10-26T00:05:52Z|1|python3 -m pytest tests/unit/x.py" ;;
      old_plain) echo "/sharp_borg|2026-10-28T09:15:44Z||python3 -m pytest tests/unit/y.py" ;;
      fresh_ut) echo "/fresh|$(date -u -d '-10 min' +%Y-%m-%dT%H:%M:%SZ)|1|timeout -k 10 1200 python3 -m pytest z" ;;
      api) echo "/aads-server|2026-10-01T00:00:00Z||supervisord -c /app/supervisord.conf" ;;
    esac ;;
  rm) echo "$3" >> "$REMOVED" ;;
esac
''')
    fake.chmod(0o755)
    removed = tmp_path / "removed.txt"
    removed.write_text("")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "REMOVED": str(removed), "LOG": str(tmp_path / "log")}
    subprocess.run(["bash", "-c", block], check=True, env=env)
    return removed.read_text().split()


def test_stale_unit_test_containers_are_reaped_before_active_image_scan(tmp_path):
    assert SOURCE.index("STALE_TEST_SECONDS=") < SOURCE.index("ACTIVE_IMAGE_IDS=")
    assert sorted(_run_stale_test_reaper(tmp_path)) == ["old_plain", "old_ut"]


def test_builder_cache_retention_is_72h():
    assert 'docker builder prune -af --filter "until=72h"' in SOURCE
    assert 'until=168h' not in SOURCE.split("prune_project_images\n", 1)[0].split("STALE_TEST_SECONDS=", 1)[1]
