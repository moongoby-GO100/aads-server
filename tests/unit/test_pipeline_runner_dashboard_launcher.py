"""Dashboard launcher routing and bounded lock retries, without real deployments."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/pipeline-runner.sh").read_text()


def function_source():
    start = SCRIPT.index("dashboard_deploy_release() {")
    return SCRIPT[start:SCRIPT.index("\n}\n", start) + 3]


@pytest.mark.parametrize(
    "mode,sha,expected,calls",
    [
        ("success", "release123", 0, 1),
        ("missing", "release123", 0, 0),
        ("nonexec", "release123", 0, 0),
        ("locked", "release123", 75, 3),
        ("unpushed", "release123", 2, 1),
        ("eventual", "rollback456", 0, 2),
        ("success", "", 0, 0),
    ],
)
def test_launcher(tmp_path, mode, sha, expected, calls):
    launcher = tmp_path / "launcher"
    trace = tmp_path / "trace"
    legacy = tmp_path / "legacy"
    launcher.write_text("""#!/bin/bash
echo "launcher:$AADS_RELEASE_SHA:$*" >> "$TRACE"
case "$MODE" in
  locked) exit 75 ;;
  unpushed) exit 2 ;;
  eventual) [[ $(wc -l < "$TRACE") -eq 1 ]] && exit 75 ;;
esac
exit 0
""")
    launcher.chmod(0o644 if mode == "nonexec" else 0o755)
    if mode == "missing":
        launcher.unlink()
    legacy.write_text('#!/bin/bash\necho "legacy:${AADS_RELEASE_SHA:-}:$*" >> "$TRACE"\n')
    source = function_source().replace(
        "/usr/local/sbin/aads-dashboard-deploy", str(launcher)
    ).replace("/root/aads/aads-dashboard/deploy.sh", str(legacy))
    result = subprocess.run(
        ["bash", "-c", 'set -eo pipefail\nlog() { echo "$*"; }\n'
         'sleep() { echo "sleep:$*" >> "$TRACE"; }\n' + source
         + '\ndashboard_deploy_release "$SHA" --probe'],
        env={**os.environ, "TRACE": str(trace), "MODE": mode, "SHA": sha},
        text=True, capture_output=True,
    )
    assert result.returncode == expected, result.stderr
    lines = trace.read_text().splitlines()
    assert sum(line.startswith("launcher:") for line in lines) == calls
    if calls == 0:
        assert lines == [f"legacy:{sha}:--probe"]
        assert "DASHBOARD_DEPLOY_FALLBACK" in result.stdout
    else:
        assert all(line == f"launcher:{sha}:--probe"
                   for line in lines if line.startswith("launcher:"))
    if mode == "locked":
        assert lines.count("sleep:5") == 2
        assert "DASHBOARD_DEPLOY_DEFERRED" in result.stdout
    if mode == "unpushed":
        assert "release sha not pushed" in result.stdout
        assert not any(line.startswith("sleep:") for line in lines)


def test_call_sites_and_failure_routing():
    assert 'dashboard_deploy_release "$current_sha"' in SCRIPT
    assert 'dashboard_deploy_release "$rollback_sha"' in SCRIPT
    assert 'aads-dashboard:launcher_locked' in SCRIPT
    assert '_dash_reason="release sha not pushed"' in SCRIPT
    assert SCRIPT.count("bash /root/aads/aads-dashboard/deploy.sh") == 2
    assert (ROOT / "scripts/pipeline-runner.sh").read_bytes() == (
        ROOT / "scripts/pipeline-runner.sh.local"
    ).read_bytes()
