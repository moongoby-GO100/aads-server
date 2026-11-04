"""운영 폴더 동기화(2026-11-04): push 뒤 비-AADS 운영 폴더를 승인 SHA 로 ff-only 이동.

오류 사전 runner.go100_deploy_not_applied_to_live — GO100 backend 변경 150건이 운영에 반영되지 않았다.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/pipeline-runner.sh").read_text(encoding="utf-8")
BLOCK = SCRIPT[SCRIPT.index("# ── 운영 폴더 동기화 함수 (2026-11-04)"):SCRIPT.index("claim_approved_job() {")]


def git(*a, cwd):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def setup(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    live = tmp_path / "live"
    subprocess.run(["git", "clone", "-q", str(origin), str(live)], check=True, capture_output=True)
    (live / "a.py").write_text("a=1\n")
    (live / "b.py").write_text("b=1\n")
    git("add", ".", cwd=live); git("commit", "-q", "-m", "base", cwd=live); git("push", "-q", "origin", "HEAD:main", cwd=live)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    (work / "a.py").write_text("a=2\n")
    git("commit", "-qam", "job", cwd=work); git("push", "-q", "origin", "HEAD:main", cwd=work)
    return live, git("rev-parse", "HEAD", cwd=work)


def run(fn_call, env):
    r = subprocess.run(["bash", "-c", f"set -u\n{BLOCK}\n{fn_call}; echo \"rc=$? reason=$RUNNER_LIVE_SYNC_REASON\""],
                       env={**os.environ, **env}, capture_output=True, text=True)
    return r.stdout.strip().splitlines()[-1]


def test_enabled_project_fast_forwards_to_approved_sha(tmp_path):
    live, sha = setup(tmp_path)
    out = run(f"runner_sync_live_workdir NTV2 {live} {sha} 'a.py'", {"RUNNER_LIVE_SYNC_PROJECTS": "SF,NTV2"})
    assert out.startswith("rc=0"), out
    assert git("rev-parse", "HEAD", cwd=live) == sha


def test_not_enabled_project_only_warns_and_does_not_touch_live(tmp_path):
    live, sha = setup(tmp_path)
    before = git("rev-parse", "HEAD", cwd=live)
    out = run(f"runner_sync_live_workdir GO100 {live} {sha} 'a.py'", {"RUNNER_LIVE_SYNC_PROJECTS": "SF,NTV2"})
    assert out == "rc=2 reason=not_enabled"
    assert git("rev-parse", "HEAD", cwd=live) == before


def test_dirty_overlap_blocks_without_touching_live(tmp_path):
    live, sha = setup(tmp_path)
    (live / "a.py").write_text("a=hotfix\n")
    before = git("rev-parse", "HEAD", cwd=live)
    out = run(f"runner_sync_live_workdir NTV2 {live} {sha} 'a.py'", {"RUNNER_LIVE_SYNC_PROJECTS": "NTV2"})
    assert out.startswith("rc=1 reason=dirty_overlap:a.py"), out
    assert git("rev-parse", "HEAD", cwd=live) == before
    assert (live / "a.py").read_text() == "a=hotfix\n"


def test_dirty_non_overlapping_file_is_allowed(tmp_path):
    live, sha = setup(tmp_path)
    (live / "b.py").write_text("b=local\n")
    out = run(f"runner_sync_live_workdir NTV2 {live} {sha} 'a.py'", {"RUNNER_LIVE_SYNC_PROJECTS": "NTV2"})
    assert out.startswith("rc=0"), out
    assert (live / "b.py").read_text() == "b=local\n"


def test_diverged_live_fails_closed(tmp_path):
    live, sha = setup(tmp_path)
    (live / "c.py").write_text("c=1\n"); git("add", "c.py", cwd=live); git("commit", "-q", "-m", "live-only", cwd=live)
    out = run(f"runner_sync_live_workdir NTV2 {live} {sha} 'a.py'", {"RUNNER_LIVE_SYNC_PROJECTS": "NTV2"})
    assert out == "rc=1 reason=ff_only_failed"


def test_already_contains_sha_is_ok_even_if_not_enabled(tmp_path):
    live, sha = setup(tmp_path)
    git("pull", "-q", "origin", "main", cwd=live)
    assert run(f"runner_sync_live_workdir GO100 {live} {sha} 'a.py'", {}) == "rc=0 reason="


def test_hook_runs_before_restart_case_and_fails_closed():
    i_hook = SCRIPT.index('runner_sync_live_workdir "$project" "$main_workdir" "$current_sha" "$_live_files"')
    i_case = SCRIPT.index('    case "$project" in\n        AADS)')
    assert i_hook < i_case
    seg = SCRIPT[i_hook:i_case]
    assert '"deploy_live_sync_failed"' in seg and "return 1" in seg
    assert "LIVE_WORKDIR_NOT_SYNCED" in seg
    assert (ROOT / "scripts/pipeline-runner.sh.local").read_text(encoding="utf-8") == SCRIPT
