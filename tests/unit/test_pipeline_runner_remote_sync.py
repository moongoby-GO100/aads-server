from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_remote_runner_sync_script_has_fail_closed_install_flow():
    script = (ROOT / "scripts" / "sync_pipeline_runner_remote.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "bash -n \"$CANONICAL_RUNNER\"" in script
    assert "bash -n '$tmp'" in script
    assert "installed hash mismatch" in script
    assert "flock -n 9" in script
    assert 'ssh -n "${SSH_OPTS[@]}" "$host" "$@"' in script
    assert "cp -p '$remote_runner'" in script
    assert "systemctl restart '$service'" in script
    assert "systemctl is-active '$service'" in script
    assert ' "$changed" == "1" || "$unit_changed" == "1" ' in script
    assert 'read -r target || [[ -n "$target" ]]' in script
    assert "return 0\n}" in script


def test_remote_runner_sync_targets_cover_all_remote_runner_hosts():
    script = (ROOT / "scripts" / "sync_pipeline_runner_remote.sh").read_text(encoding="utf-8")

    assert "contabo14|contabo14|/root/scripts/pipeline-runner.sh|aads-pipeline-runner.service" in script
    assert "cafe24_114|server-114|/root/scripts/pipeline-runner.sh|aads-pipeline-litellm-runner.service" in script
    assert "aads-pipeline-litellm-runner.211.service" in script
    assert "aads-pipeline-litellm-runner.114.service" in script


def test_remote_runner_sync_timer_runs_periodically():
    timer = (ROOT / "scripts" / "aads-pipeline-runner-sync.timer").read_text(encoding="utf-8")
    service = (ROOT / "scripts" / "aads-pipeline-runner-sync.service").read_text(encoding="utf-8")

    assert "OnBootSec=2min" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer
    assert "ExecStart=/root/aads/aads-server/scripts/sync_pipeline_runner_remote.sh" in service
