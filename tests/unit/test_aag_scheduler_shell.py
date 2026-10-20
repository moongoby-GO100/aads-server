"""aag_go100_refresh.sh / aag_all_projects_refresh.sh 를 스텁 ssh/scp 로 실제 실행한다.

네트워크·systemctl·운영 DB 는 건드리지 않는다. 시간 의존 장중 게이트는 `--now` 를 주입하는
래퍼(AAG_SCHEDULER_STATE_PY)로 고정한다.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHA = "c" * 40
OFF_HOURS = "2026-10-20T12:00:00Z"   # Tue 21:00 KST
MARKET = "2026-10-20T01:00:00Z"      # Tue 10:00 KST

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None or shutil.which("bash") is None,
    reason="flock/bash required",
)


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


SSH_STUB = f"""#!/usr/bin/env bash
case "$*" in
  *rev-parse*) echo {SHA};;
  *scan_aads.py*) echo "scan stderr" >&2; exit "${{STUB_SCAN_RC:-0}}";;
  *"test "*) exit "${{STUB_ARTIFACT_RC:-0}}";;
esac
exit 0
"""

SCP_STUB = f"""#!/usr/bin/env bash
dest="${{@: -1}}"
if [[ "$*" == *":/"*"/out/go100-graph.json"* && -d "$dest" ]]; then
  cat >"$dest/go100-graph.json" <<JSON
{{"commit_sha": "${{STUB_GRAPH_SHA:-{SHA}}}", "generated_at": "2026-10-20T12:00:00Z",
  "findings": [{{"severity": "P1"}}], "stats": {{"mounted_routes": 3}}}}
JSON
  echo x >"$dest/go100-findings.md"; echo x >"$dest/go100-arch.mmd"
fi
exit 0
"""

PUSH_STUB = """import os, sys
with open(os.environ["STUB_PUSH_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("STUB_PUSH_RC", "0")))
"""

NOW_WRAPPER = """import os, sys
sys.path.insert(0, {repo!r})
from scripts import aag_scheduler_state as s
raise SystemExit(s.main(["--now", os.environ["AAG_TEST_NOW"]] + sys.argv[1:]))
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", SSH_STUB), ("scp", SCP_STUB), ("systemctl", "#!/bin/sh\nexit 0\n")):
        _exe(bin_dir / name, body)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tools" / "aag").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "aag_scheduler_state.py", repo / "scripts")
    (repo / "scripts" / "aag_snapshot_push.py").write_text(PUSH_STUB)
    wrapper = tmp_path / "now_wrapper.py"
    wrapper.write_text(NOW_WRAPPER.format(repo=str(REPO)))
    values = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AAG_STATE_DIR": str(tmp_path / "state"),
        "AAG_GO100_REPO_DIR": str(repo),
        "AAG_REPO_DIR": str(repo),
        "AAG_GO100_OUT_DIR": str(tmp_path / "out"),
        "AAG_GO100_LOG": str(tmp_path / "go100.log"),
        "AAG_GO100_TMP_DIR": str(tmp_path),
        "AAG_ALL_PROJECTS_LOG": str(tmp_path / "all.log"),
        "AAG_SCHEDULER_STATE_PY": str(wrapper),
        "AAG_TEST_NOW": OFF_HOURS,
        "STUB_PUSH_LOG": str(tmp_path / "push.log"),
    }
    return tmp_path, repo, values


def _run_go100(values, **extra):
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "aag_go100_refresh.sh")],
        env={**values, **extra}, capture_output=True, text=True, timeout=120,
    )


def _state(tmp_path, project):
    path = tmp_path / "state" / "scheduler" / f"{project}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _log(tmp_path, name="go100.log"):
    path = tmp_path / name
    return path.read_text() if path.exists() else ""


# ---- GO100 refresh ---------------------------------------------------------------
def test_go100_success_exit_0_and_records_freshness_evidence(env):
    tmp_path, _, values = env
    proc = _run_go100(values)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    state = _state(tmp_path, "GO100")
    assert state["status"] == "success" and state["last_success_at"]
    assert "SNAPSHOT_PUSHED" in _log(tmp_path)
    assert "GO100=" in (tmp_path / "push.log").read_text()


def test_go100_market_window_is_exit_7_without_any_scan(env):
    tmp_path, _, values = env
    proc = _run_go100(values, AAG_TEST_NOW=MARKET)
    assert proc.returncode == 7
    assert "SKIPPED reason=market_window" in _log(tmp_path)
    assert not (tmp_path / "push.log").exists()
    assert not (tmp_path / "out" / "go100-graph.json").exists()
    state = _state(tmp_path, "GO100")
    assert state["status"] == "skipped" and state["last_success_at"] is None


def test_go100_duplicate_run_is_exit_7_not_0(env):
    tmp_path, _, values = env
    lock = tmp_path / "aag-go100-refresh.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "20"])
    try:
        for _ in range(50):
            if lock.exists():
                break
            subprocess.run(["sleep", "0.1"])
        proc = _run_go100(values)
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert "SKIPPED reason=already_running" in _log(tmp_path)
    assert _state(tmp_path, "GO100")["last_skip_reason"] == "already_running"
    assert _state(tmp_path, "GO100")["last_success_at"] is None


def test_go100_ingest_failure_is_exit_8_and_not_success(env):
    tmp_path, _, values = env
    proc = _run_go100(values, STUB_PUSH_RC="1")
    assert proc.returncode == 8
    state = _state(tmp_path, "GO100")
    assert state["status"] == "failed" and state["last_success_at"] is None
    assert state["last_failure_reason"] == "snapshot_push_failed"
    assert "SNAPSHOT_PUSH_FAILED rc=8" in _log(tmp_path)


def test_go100_scan_failure_is_exit_1_and_records_reason(env):
    tmp_path, _, values = env
    proc = _run_go100(values, STUB_SCAN_RC="2")
    assert proc.returncode == 1
    state = _state(tmp_path, "GO100")
    assert state["status"] == "failed" and "scan_failed rc=2" in state["last_failure_reason"]
    assert not (tmp_path / "push.log").exists()


def test_go100_findings_present_scan_rc1_still_succeeds(env):
    tmp_path, _, values = env
    assert _run_go100(values, STUB_SCAN_RC="1").returncode == 0
    assert _state(tmp_path, "GO100")["status"] == "success"


def test_go100_stale_artifact_and_sha_mismatch_fail_closed(env):
    tmp_path, _, values = env
    assert _run_go100(values, STUB_ARTIFACT_RC="1").returncode == 1
    assert _state(tmp_path, "GO100")["last_failure_reason"] == "stale_artifact"
    assert _run_go100(values, STUB_GRAPH_SHA="d" * 40).returncode == 1
    assert _state(tmp_path, "GO100")["last_failure_reason"] == "sha_mismatch"
    assert not (tmp_path / "push.log").exists()


def test_go100_broken_scheduler_fails_closed_instead_of_scanning(env):
    tmp_path, repo, values = env
    broken = tmp_path / "broken.py"
    broken.write_text("raise SystemExit(1)\n")
    proc = _run_go100(values, AAG_SCHEDULER_STATE_PY=str(broken))
    assert proc.returncode == 1
    assert "SCHEDULER_STATE_ERROR" in _log(tmp_path)
    assert not (tmp_path / "push.log").exists()


def test_go100_skip_never_overwrites_previous_success(env):
    tmp_path, _, values = env
    assert _run_go100(values).returncode == 0
    first = _state(tmp_path, "GO100")["last_success_at"]
    assert _run_go100(values, AAG_TEST_NOW=MARKET).returncode == 7
    state = _state(tmp_path, "GO100")
    assert state["status"] == "skipped" and state["last_success_at"] == first


# ---- all-projects refresh -----------------------------------------------------
GO100_STUB = """#!/usr/bin/env bash
rc="${STUB_GO100_RC:-0}"
if [[ "$rc" == 7 ]]; then
  python3 "$AAG_REPO_DIR/scripts/aag_scheduler_state.py" --state-dir "$AAG_STATE_DIR" \\
      skip GO100 --reason market_window >/dev/null
fi
exit "$rc"
"""


def _run_all(values, **extra):
    repo = Path(values["AAG_REPO_DIR"])
    _exe(repo / "scripts" / "aag_go100_refresh.sh", GO100_STUB)
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "aag_all_projects_refresh.sh")],
        env={**values, **extra}, capture_output=True, text=True, timeout=180,
    )


def test_all_projects_go100_skip_is_not_success_and_kis_not_republished(env):
    tmp_path, _, values = env
    proc = _run_all(values, STUB_GO100_RC="7")
    log = _log(tmp_path, "all.log")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "STATUS project=GO100 result=skipped reason=market_window" in log
    assert "STATUS project=KIS result=skipped reason=upstream_go100_skipped" in log
    assert "STATUS project=GO100 result=success" not in log
    assert "DONE result=partial_skipped" in log and "result=success projects=6" not in log
    pushed = (tmp_path / "push.log").read_text()
    assert "KIS@GO100" not in pushed and "GO100=" not in pushed
    assert _state(tmp_path, "ALL")["status"] == "partial"
    assert _state(tmp_path, "ALL")["last_success_at"] is None


def test_all_projects_full_success(env):
    tmp_path, _, values = env
    proc = _run_all(values, STUB_GO100_RC="0")
    log = _log(tmp_path, "all.log")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DONE result=success projects=6" in log
    assert "KIS@GO100=" in (tmp_path / "push.log").read_text()
    assert _state(tmp_path, "ALL")["status"] == "success"


@pytest.mark.parametrize("rc", ["1", "8"])
def test_all_projects_go100_failure_is_failed_and_exit_1(env, rc):
    tmp_path, _, values = env
    proc = _run_all(values, STUB_GO100_RC=rc)
    log = _log(tmp_path, "all.log")
    assert proc.returncode == 1
    assert f"STATUS project=GO100 result=failed detail=remote_scan_or_publish rc={rc}" in log
    assert "DONE result=failed projects=GO100" in log
    assert "KIS@GO100" not in (tmp_path / "push.log").read_text()
    assert _state(tmp_path, "ALL")["status"] == "failed"


def test_all_projects_duplicate_run_is_skipped_status_not_success(env):
    tmp_path, _, values = env
    lock = tmp_path / "all.lock"
    values = {**values, "AAG_ALL_PROJECTS_LOCK": str(lock)}
    holder = subprocess.Popen(["flock", str(lock), "sleep", "20"])
    try:
        for _ in range(50):
            if lock.exists():
                break
            subprocess.run(["sleep", "0.1"])
        proc = _run_all(values)
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    assert proc.returncode == 0
    log = _log(tmp_path, "all.log")
    assert "STATUS project=ALL result=skipped reason=already_running" in log
    assert "result=success" not in log
    assert not (tmp_path / "push.log").exists()


def test_scripts_pass_bash_syntax_check():
    for name in ("aag_go100_refresh.sh", "aag_all_projects_refresh.sh"):
        assert subprocess.run(["bash", "-n", str(REPO / "scripts" / name)]).returncode == 0
    assert sys.version_info >= (3, 9)
