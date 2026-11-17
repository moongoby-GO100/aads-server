import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/run_unit_tests.sh"


def test_daemon_unavailable_exits_before_candidate_lookup(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/bash\necho "$*" >> "$DOCKER_CALLS"\necho "permission denied" >&2\nexit 1\n')
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "DOCKER_CALLS": str(calls)}
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "docker 데몬 접근 불가" in result.stderr
    assert "러너 본체가 샌드박스 밖에서" in result.stderr
    assert calls.read_text().splitlines() == ["info"]


def test_latest_image_across_registry_and_local_filters(tmp_path):
    source = SCRIPT.read_text()
    start = source.index("resolve_test_image_candidates() {")
    end = source.index("\n}\n", start) + 3
    docker = tmp_path / "docker"
    docker.write_text('''#!/bin/bash
case "$1" in
  ps) ;;
  images)
    case "$*" in
      *'reference=*/aads-server:*'*) printf '2026-11-17 10:00:00 +0000 UTC\\tlocalhost:5000/aads-server:new\\n' ;;
      *'reference=aads-server:*'*) printf '2026-11-04 10:00:00 +0000 UTC\\taads-server:old\\n' ;;
      *) exit 1 ;;
    esac ;;
  *) exit 1 ;;
esac
''')
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "ACTIVE_CONTAINER_FILE": str(tmp_path / "absent")}
    env.pop("AADS_TEST_IMAGE_SOURCE", None)
    result = subprocess.run(["bash", "-c", source[start:end] + "\nresolve_test_image_candidates"], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["aads-server", "image:localhost:5000/aads-server:new"]
