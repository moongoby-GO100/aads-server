"""AADS-RUNNER-WT-RECLAIM-ON-FINISH-20261005

2026-10-05 contabo14: /tmp/aads-wt-runner-* 52개(11.5GB)가 쌓였다. 그 서버에는
reclaim_runner_worktrees.sh 가 배치되지 않아 5분 주기 정리가 "reclaimer unavailable" 만 181회/일 찍었다.

여기서는 실제 git 저장소/워크트리로 즉시 회수 모드를 돌리고, 러너 함수는 스크립트에서 잘라 내
가짜 db/log/curl 로 실행한다.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECLAIM = ROOT / "scripts" / "reclaim_runner_worktrees.sh"
RUNNER = ROOT / "scripts" / "pipeline-runner.sh"
SYNC = ROOT / "scripts" / "sync_pipeline_runner_remote.sh"
LAUNCHER = ROOT / "scripts" / "runner_sync_launcher.sh"

TERMINAL = ["done", "error", "cancelled", "rejected_done", "failed"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "Test")
    _git(r, "config", "user.email", "test@example.com")
    (r / "README").write_text("test\n")
    _git(r, "add", "README")
    _git(r, "commit", "-qm", "initial")
    _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    return r


@pytest.fixture
def make_wt(repo):
    created: list[Path] = []

    def _make() -> tuple[str, Path]:
        job_id = f"runner-{uuid.uuid4().hex[:8]}"
        path = Path(f"/tmp/aads-wt-{job_id}")
        _git(repo, "worktree", "add", "--detach", str(path), "HEAD")
        created.append(path)
        return job_id, path

    yield _make
    for p in created:
        shutil.rmtree(p, ignore_errors=True)
    _git(repo, "worktree", "prune")


def _bin(tmp_path: Path, busy_path: str = "") -> Path:
    """lsof/docker/psql 가짜. psql 은 항상 실패 — 즉시 회수 모드는 DB 를 읽지 않아야 한다."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    lsof = b / "lsof"
    if busy_path:
        lsof.write_text(f'#!/bin/sh\ncase "$2" in {busy_path}) exit 0 ;; esac\nexit 1\n')
    else:
        lsof.write_text("#!/bin/sh\nexit 1\n")
    lsof.chmod(0o755)
    for name in ("docker", "psql"):
        (b / name).write_text("#!/bin/sh\nexit 1\n")
        (b / name).chmod(0o755)
    return b


def _immediate(repo, tmp_path, job_id, status, *, dry_run=False, busy_path=""):
    env = os.environ | {
        "PATH": f"{_bin(tmp_path, busy_path)}:{os.environ['PATH']}",
        "RUNNER_WT_REPO": str(repo),
        "RUNNER_WT_ENV_FILE": str(tmp_path / "runner.env"),
        "RUNNER_WT_ARCHIVE_DIR": str(tmp_path / "archive"),
        "RUNNER_WT_ONLY_JOB": job_id,
        "RUNNER_WT_ONLY_STATUS": status,
        "DRY_RUN": "1" if dry_run else "0",
    }
    return subprocess.run(
        ["bash", str(RECLAIM)], env=env, text=True, capture_output=True, timeout=60, check=False
    )


def _result(proc) -> str:
    last = proc.stdout.strip().splitlines()[-1]
    m = re.search(r"result=(\S+)", last)
    assert m, proc.stdout + proc.stderr
    return m.group(1)


# ── 회수 스크립트: 즉시 회수 모드 ────────────────────────────────────────


@pytest.mark.parametrize("status", TERMINAL)
def test_clean_worktree_is_removed_immediately_for_every_terminal_status(repo, make_wt, tmp_path, status):
    job_id, path = make_wt()

    proc = _immediate(repo, tmp_path, job_id, status)

    assert proc.returncode == 0, proc.stderr
    assert not path.exists()
    assert _result(proc) == "removed"
    assert "reclaimed=1" in proc.stdout
    # 깨끗하면 아카이브할 것이 없다
    assert not (tmp_path / "archive").exists()
    # DB 를 읽지 않는다(psql 은 항상 실패하도록 해 두었다)
    assert "DB unavailable" not in proc.stderr


def test_uncommitted_change_is_preserved_without_archiving(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    proc = _immediate(repo, tmp_path, job_id, "error")

    assert path.is_dir()
    assert (path / "README").read_text() == "edited\n"
    assert _result(proc) == "preserved"
    assert "preserved_unclean=1" in proc.stdout
    assert not (tmp_path / "archive").exists()


def test_untracked_file_is_preserved(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "new.txt").write_text("x\n")

    proc = _immediate(repo, tmp_path, job_id, "done")

    assert path.is_dir()
    assert _result(proc) == "preserved"


def test_ignored_file_is_preserved_like_the_existing_guard(repo, make_wt, tmp_path):
    (repo / ".git" / "info" / "exclude").write_text("*.cache\n")
    job_id, path = make_wt()
    (path / "build.cache").write_text("x\n")

    proc = _immediate(repo, tmp_path, job_id, "done")

    assert path.is_dir()
    assert _result(proc) == "preserved"
    assert "preserved_unclean=1" in proc.stdout


def _ignore_caches(repo):
    (repo / ".git" / "info" / "exclude").write_text(
        "__pycache__/\n.ruff_cache/\n.pytest_cache/\n.next/\ntsconfig.tsbuildinfo\n*.pyc\n.vault.key\n"
    )


def test_regenerable_ignored_caches_do_not_block_reclaim(repo, make_wt, tmp_path):
    _ignore_caches(repo)
    job_id, path = make_wt()
    for rel in ("app/__pycache__/x.pyc", ".ruff_cache/a", ".pytest_cache/b", ".next/c/d", "tsconfig.tsbuildinfo"):
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n")

    proc = _immediate(repo, tmp_path, job_id, "done")

    assert not path.exists()
    assert _result(proc) == "removed"


def test_cache_allowlist_does_not_hide_other_ignored_or_untracked_files(repo, make_wt, tmp_path):
    _ignore_caches(repo)
    job_id, path = make_wt()
    (path / ".ruff_cache").mkdir()
    (path / ".ruff_cache" / "a").write_text("x\n")
    (path / ".vault.key").write_text("secret\n")

    proc = _immediate(repo, tmp_path, job_id, "done")

    assert (path / ".vault.key").read_text() == "secret\n"
    assert _result(proc) == "preserved"

    job2, path2 = make_wt()
    (path2 / ".ruff_cache").mkdir()
    (path2 / ".ruff_cache" / "a").write_text("x\n")
    (path2 / "new.txt").write_text("x\n")
    assert _result(_immediate(repo, tmp_path, job2, "done")) == "preserved"
    assert path2.is_dir()


def test_unmerged_commit_is_preserved_without_bundling(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "feature.txt").write_text("work\n")
    _git(path, "add", "feature.txt")
    _git(path, "commit", "-qm", "unpushed work")

    proc = _immediate(repo, tmp_path, job_id, "error")

    assert path.is_dir()
    assert _result(proc) == "preserved"
    assert "preserved_unclean=1" in proc.stdout
    assert not (tmp_path / "archive").exists()


def test_merged_commit_does_not_count_as_unmerged(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "feature.txt").write_text("work\n")
    _git(path, "add", "feature.txt")
    _git(path, "commit", "-qm", "pushed work")
    _git(repo, "update-ref", "refs/remotes/origin/main", _git(path, "rev-parse", "HEAD").strip())

    proc = _immediate(repo, tmp_path, job_id, "done")

    assert not path.exists()
    assert _result(proc) == "removed"


@pytest.mark.parametrize("status", ["awaiting_approval", "approved", "deploying", "review_hold", "running", "queued", ""])
def test_non_terminal_status_is_never_reclaimed(repo, make_wt, tmp_path, status):
    job_id, path = make_wt()

    proc = _immediate(repo, tmp_path, job_id, status)

    assert proc.returncode == 0, proc.stderr
    assert path.is_dir()
    assert _result(proc) == "preserved_status"
    assert "reclaimed=0" in proc.stdout


def test_active_process_in_worktree_preserves_it(repo, make_wt, tmp_path):
    job_id, path = make_wt()

    proc = _immediate(repo, tmp_path, job_id, "done", busy_path=str(path))

    assert path.is_dir()
    assert _result(proc) == "preserved"
    assert "skipped_active=1" in proc.stdout


def test_dry_run_removes_nothing_and_reports_candidate(repo, make_wt, tmp_path):
    job_id, path = make_wt()

    proc = _immediate(repo, tmp_path, job_id, "done", dry_run=True)

    assert path.is_dir()
    assert _result(proc) == "dry_run_candidate"
    assert f"DRY_RUN candidate={path}" in proc.stdout


def test_missing_worktree_and_invalid_job_id(repo, tmp_path):
    proc = _immediate(repo, tmp_path, f"runner-{uuid.uuid4().hex[:8]}", "done")
    assert proc.returncode == 0
    assert _result(proc) == "absent"

    for bad in ("../etc", "runner-a b", "notrunner-1", "runner-$(id)"):
        bad_proc = _immediate(repo, tmp_path, bad, "done")
        assert bad_proc.returncode == 2, bad
        assert "invalid RUNNER_WT_ONLY_JOB" in bad_proc.stderr


def test_unregistered_directory_is_not_touched(repo, tmp_path):
    job_id = f"runner-{uuid.uuid4().hex[:8]}"
    path = Path(f"/tmp/aads-wt-{job_id}")
    path.mkdir()
    (path / "keep.txt").write_text("x")
    try:
        proc = _immediate(repo, tmp_path, job_id, "done")
        assert (path / "keep.txt").exists()
        assert _result(proc) == "preserved_unverified"
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ── 러너 함수: 종료 훅 / 부재 경고 ──────────────────────────────────────


def _extract(name: str) -> str:
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index(f"\n{name}() {{\n") + 1
    end = text.index("\n}\n", start) + 3
    return text[start:end]


@pytest.fixture
def harness(tmp_path, repo):
    """러너에서 잘라 낸 함수를 tmp 디렉터리의 가짜 러너 파일로 source 한다(BASH_SOURCE 기준 경로 해석 유지)."""
    run_dir = tmp_path / "runner_dir"
    run_dir.mkdir()
    funcs = "\n".join(
        _extract(n)
        for n in ("_reclaimer_script_path", "_warn_reclaimer_unavailable", "_reclaim_finished_worktree")
    )
    (run_dir / "pipeline-runner.sh").write_text(funcs + "\n")
    log = tmp_path / "harness.log"
    log.write_text("")

    def run(body: str, *, status: str = "done", install_reclaimer: bool = True, extra_env: dict | None = None):
        target = run_dir / "reclaim_runner_worktrees.sh"
        if install_reclaimer:
            if not target.exists():
                shutil.copy(RECLAIM, target)
                target.chmod(0o755)
        elif target.exists():
            target.unlink()
        script = f"""
LOGF={log}
log() {{ echo "LOG $*" >> "$LOGF"; }}
db_exec() {{ echo "{status}"; }}
sql_escape() {{ echo "'$1'"; }}
lookup_error_book() {{ echo "ERRBOOK $2 :: $(cat "$1")" >> "$LOGF"; }}
curl() {{ echo "CURL $*" >> "$LOGF"; }}
export TELEGRAM_BOT_TOKEN=tok TELEGRAM_CHAT_ID=chat RUNNER_HOST_NAME=testhost
export RECLAIMER_MISSING_FLAG={tmp_path}/missing.flag
source {run_dir}/pipeline-runner.sh
{body}
"""
        env = os.environ | {
            "PATH": f"{_bin(tmp_path)}:{os.environ['PATH']}",
            "RUNNER_WT_REPO": str(repo),
            "RUNNER_WT_ENV_FILE": str(tmp_path / "runner.env"),
            "RUNNER_WT_ARCHIVE_DIR": str(tmp_path / "archive"),
        } | (extra_env or {})
        proc = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        return log.read_text()

    return run


def test_runner_hook_removes_clean_terminal_worktree(harness, make_wt):
    job_id, path = make_wt()

    out = harness(f"_reclaim_finished_worktree {job_id}", status="done")

    assert not path.exists()
    assert f"WORKTREE_RECLAIM job={job_id}" in out
    assert "result=removed" in out


def test_runner_hook_preserves_dirty_error_worktree(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    out = harness(f"_reclaim_finished_worktree {job_id}", status="error")

    assert path.is_dir()
    assert "result=preserved" in out


def test_runner_hook_preserves_unmerged_commit(harness, make_wt):
    job_id, path = make_wt()
    (path / "f.txt").write_text("w\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-qm", "work")

    out = harness(f"_reclaim_finished_worktree {job_id}", status="error")

    assert path.is_dir()
    assert "result=preserved" in out


@pytest.mark.parametrize("status", ["awaiting_approval", "review_hold", "running", ""])
def test_runner_hook_never_touches_awaiting_approval_or_other_non_terminal(harness, make_wt, status):
    job_id, path = make_wt()

    out = harness(f"_reclaim_finished_worktree {job_id}", status=status)

    assert path.is_dir()
    assert "WORKTREE_RECLAIM_SKIP" in out
    assert "result=" not in out


def test_runner_hook_can_be_disabled(harness, make_wt):
    job_id, path = make_wt()

    harness(f"_reclaim_finished_worktree {job_id}", extra_env={"RUNNER_WT_IMMEDIATE_RECLAIM": "0"})

    assert path.is_dir()


def test_runner_hook_ignores_non_runner_job_ids(harness):
    out = harness("_reclaim_finished_worktree 'not-a-runner;rm -rf /'")
    assert out == ""


def test_missing_reclaimer_warns_once_with_telegram_and_error_book(harness, make_wt, tmp_path):
    jobs = [make_wt() for _ in range(3)]
    body = "\n".join(f"_reclaim_finished_worktree {j}" for j, _ in jobs)
    body += "\n_warn_reclaimer_unavailable \"$(_reclaimer_script_path)\"\n"

    out = harness(body, status="done", install_reclaimer=False)

    assert all(p.is_dir() for _, p in jobs)
    assert out.count("WARN STALE_WORKTREE_CLEANUP: reclaimer unavailable") == 1
    assert out.count("CURL ") == 1 and "chat_id=chat" in out and "testhost" in out
    assert out.count("ERRBOOK ") == 1 and "reclaimer unavailable" in out
    assert (tmp_path / "missing.flag").exists()


def test_missing_reclaimer_warns_again_after_interval(harness, tmp_path):
    body = '_warn_reclaimer_unavailable "$(_reclaimer_script_path)"'
    harness(body, install_reclaimer=False)
    flag = tmp_path / "missing.flag"
    old = time.time() - 3 * 86400
    os.utime(flag, (old, old))

    out = harness(body, install_reclaimer=False)

    assert out.count("WARN STALE_WORKTREE_CLEANUP") == 2


def test_cleanup_old_artifacts_uses_warning_helper_instead_of_silent_log():
    body = _extract("_cleanup_old_artifacts")
    assert "_warn_reclaimer_unavailable" in body
    assert 'log "  STALE_WORKTREE_CLEANUP: reclaimer unavailable' not in body


def test_reap_bg_jobs_hooks_every_finished_job():
    body = _extract("_reap_bg_jobs")
    assert '_reclaim_finished_worktree "${_bg_jobs[$_pid]%%|*}"' in body
    assert body.index("_reclaim_finished_worktree") < body.index("unset '_bg_jobs[$_pid]'")


def test_hook_and_reclaimer_share_the_same_terminal_status_set():
    terminal = "done|error|cancelled|rejected_done|failed"
    hook_cases = _extract("_reclaim_finished_worktree").split("case", 1)[1].split("esac", 1)[0]
    assert terminal in hook_cases
    assert "awaiting_approval" not in hook_cases
    assert terminal in RECLAIM.read_text(encoding="utf-8")


# ── 원격 배치 경로 ──────────────────────────────────────────────────────


def test_launcher_exports_and_requires_the_reclaimer():
    text = LAUNCHER.read_text(encoding="utf-8")
    required = text.split("REQUIRED_FILES=(", 1)[1].split(")", 1)[0]
    assert "scripts/reclaim_runner_worktrees.sh" in required


def test_sync_installs_reclaimer_next_to_runner_with_exec_mode():
    # 39927ed6: 개별 install 대신 단일 번들(manifest)로 적용한다.
    text = SYNC.read_text(encoding="utf-8")
    assert "reclaim = script_dir / 'reclaim_runner_worktrees.sh'" in text
    # 러너와 같은 디렉터리, 실행 권한 0755, 재시작 불필요(False)
    assert "rows.append((reclaim, directory / reclaim.name, 0o755, False))" in text
    # 원본이 없으면(옛 런처) 번들에서 빠질 뿐 실패하지 않는다
    assert "if reclaim.is_file():" in text
    # 원격 적용 측 허용 목록과 .sh 구문 검사
    assert "'reclaim_runner_worktrees.sh', 'aag-brief.py'" in text
    assert "row['destination'].endswith('.sh')" in text
    assert "['bash', '-n', str(staged)]" in text
