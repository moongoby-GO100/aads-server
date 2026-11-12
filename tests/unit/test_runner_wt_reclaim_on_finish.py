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

from app.services.pipeline_auto_rework import REWORK_CANDIDATE_SQL_EXPR, is_request_changes_failure

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


def _immediate(
    repo,
    tmp_path,
    job_id,
    status,
    *,
    dry_run=False,
    busy_path="",
    phase="",
    detail="",
    is_rework: int | str | None = None,
    phase_env=True,
):
    if is_rework is None:
        rework_flag = "1" if is_request_changes_failure(status, phase, detail) else "0"
    else:
        rework_flag = str(is_rework)
    env = os.environ | {
        "PATH": f"{_bin(tmp_path, busy_path)}:{os.environ['PATH']}",
        "RUNNER_WT_REPO": str(repo),
        "RUNNER_WT_ENV_FILE": str(tmp_path / "runner.env"),
        "RUNNER_WT_ARCHIVE_DIR": str(tmp_path / "archive"),
        "RUNNER_WT_ONLY_JOB": job_id,
        "RUNNER_WT_ONLY_STATUS": status,
        "RUNNER_WT_ONLY_PHASE": phase,
        "RUNNER_WT_ONLY_ERROR_DETAIL": detail,
        "RUNNER_WT_ONLY_IS_REWORK": rework_flag,
        "DRY_RUN": "1" if dry_run else "0",
    }
    if not phase_env:
        env.pop("RUNNER_WT_ONLY_PHASE")
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


def test_uncommitted_change_is_preserved_without_archiving_for_done(repo, make_wt, tmp_path):
    """done 은 종전대로 깨끗한 것만 지운다."""
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    proc = _immediate(repo, tmp_path, job_id, "done")

    assert path.is_dir()
    assert (path / "README").read_text() == "edited\n"
    assert _result(proc) == "preserved"
    assert "preserved_unclean=1" in proc.stdout
    assert not (tmp_path / "archive").exists()


@pytest.mark.parametrize("status", ["error", "cancelled", "failed"])
def test_unfinished_terminal_status_archives_dirty_work_then_removes(repo, make_wt, tmp_path, status):
    """2026-11-12 contabo14 runner-7696f793/63253416: error/cancelled 는 미커밋 변경 때문에 즉시 회수가 보존했다."""
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    (path / "new_doc.md").write_text("untracked content\n")

    proc = _immediate(repo, tmp_path, job_id, status)

    assert proc.returncode == 0, proc.stderr
    assert not path.exists()
    assert _result(proc) == "removed"
    arch = tmp_path / "archive"
    full = arch / f"{job_id}.dirty-full.patch"
    assert full.is_file()
    body = full.read_text()
    # 미추적 파일 내용까지 패치에 있어야 한다(diff HEAD 만으로는 사라진다)
    assert "untracked content" in body and "new_doc.md" in body and "edited" in body
    assert (arch / f"{job_id}.dirty.patch").is_file()


def test_dirty_archive_does_not_touch_worktree_index(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "new_doc.md").write_text("x\n")
    proc = _immediate(repo, tmp_path, job_id, "error", dry_run=True)
    assert _result(proc) == "dry_run_candidate"
    assert path.is_dir()
    assert "?? new_doc.md" in _git(path, "status", "--porcelain")


def test_dirty_error_is_preserved_when_archive_flag_off(repo, make_wt, tmp_path, monkeypatch):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    monkeypatch.setenv("RUNNER_WT_IMMEDIATE_ARCHIVE_DIRTY", "0")

    proc = _immediate(repo, tmp_path, job_id, "error")

    assert path.is_dir()
    assert _result(proc) == "preserved"
    assert not (tmp_path / "archive").exists()


def test_dirty_error_is_preserved_when_patch_exceeds_limit(repo, make_wt, tmp_path, monkeypatch):
    job_id, path = make_wt()
    (path / "big.bin").write_bytes(b"a" * (2 * 1024 * 1024))
    monkeypatch.setenv("RUNNER_WT_ARCHIVE_MAX_PATCH_MB", "1")

    proc = _immediate(repo, tmp_path, job_id, "error")

    assert path.is_dir()
    assert _result(proc) == "preserved"
    assert "skipped_archive_failed=1" in proc.stdout
    # 숨김 staging(.<job>.XXXXXX)까지 포함해 job 관련 잔여물이 0개여야 한다
    arch = tmp_path / "archive"
    leftovers = [n for n in (os.listdir(arch) if arch.exists() else []) if job_id in n]
    assert leftovers == []


@pytest.mark.parametrize("status", TERMINAL)
def test_preserve_marker_blocks_reclaim_for_every_status(repo, make_wt, tmp_path, status):
    job_id, path = make_wt()
    (path / ".preserve").write_text("keep\n")

    proc = _immediate(repo, tmp_path, job_id, status)

    assert path.is_dir()
    assert _result(proc) == "preserved_marker"


def test_dirty_error_in_use_by_process_is_preserved(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    proc = _immediate(repo, tmp_path, job_id, "error", busy_path=str(path))

    assert path.is_dir()
    assert _result(proc) == "preserved"
    assert "skipped_active=1" in proc.stdout


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


def test_unmerged_commit_is_preserved_without_bundling_for_done(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "feature.txt").write_text("work\n")
    _git(path, "add", "feature.txt")
    _git(path, "commit", "-qm", "unpushed work")

    proc = _immediate(repo, tmp_path, job_id, "done")

    assert path.is_dir()
    assert _result(proc) == "preserved"
    assert "preserved_unclean=1" in proc.stdout
    assert not (tmp_path / "archive").exists()


def test_unmerged_commit_on_error_is_bundled_then_removed(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "feature.txt").write_text("work\n")
    _git(path, "add", "feature.txt")
    _git(path, "commit", "-qm", "unpushed work")

    proc = _immediate(repo, tmp_path, job_id, "error")

    assert not path.exists()
    assert _result(proc) == "removed"
    assert (tmp_path / "archive" / f"{job_id}.bundle").is_file()


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

    def run(
        body: str,
        *,
        status: str = "done",
        phase: str = "",
        detail: str = "",
        is_rework: int | None = None,
        meta_fail: bool = False,
        meta_raw: str | None = None,
        install_reclaimer: bool = True,
        extra_env: dict | None = None,
    ):
        target = run_dir / "reclaim_runner_worktrees.sh"
        if install_reclaimer:
            if not target.exists():
                shutil.copy(RECLAIM, target)
                target.chmod(0o755)
        elif target.exists():
            target.unlink()

        if meta_raw is not None:
            escaped_meta = meta_raw.replace("'", "'\\''")
            db_meta_response = f"printf '%s\\n' '{escaped_meta}'"
        elif meta_fail:
            db_meta_response = "return 1"
        else:
            rework_val = (
                is_rework
                if is_rework is not None
                else (1 if is_request_changes_failure(status, phase, detail) else 0)
            )
            db_meta_response = f'printf "%s\\x1e%s\\n" "{phase}" "{rework_val}"'

        script = f"""
LOGF={log}
log() {{ echo "LOG $*" >> "$LOGF"; }}
db_exec() {{
    case "$1" in
        *"SELECT COALESCE(phase"*) {db_meta_response} ;;
        *) echo "{status}" ;;
    esac
}}
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


def test_runner_hook_archives_then_removes_dirty_error_worktree(harness, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    out = harness(f"_reclaim_finished_worktree {job_id}", status="error")

    assert not path.exists()
    assert "result=removed" in out
    assert (tmp_path / "archive" / f"{job_id}.dirty-full.patch").is_file()


def test_runner_hook_preserves_dirty_error_worktree_when_flag_off(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    out = harness(
        f"_reclaim_finished_worktree {job_id}",
        status="error",
        extra_env={"RUNNER_WT_IMMEDIATE_ARCHIVE_DIRTY": "0"},
    )

    assert path.is_dir()
    assert "result=preserved" in out


def test_runner_hook_preserves_dirty_done_worktree(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    out = harness(f"_reclaim_finished_worktree {job_id}", status="done")

    assert path.is_dir()
    assert "result=preserved" in out


def test_runner_hook_bundles_then_removes_unmerged_error_worktree(harness, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "f.txt").write_text("w\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-qm", "work")

    out = harness(f"_reclaim_finished_worktree {job_id}", status="error")

    assert not path.exists()
    assert "result=removed" in out
    assert (tmp_path / "archive" / f"{job_id}.bundle").is_file()


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


# ── R2: ignored 비캐시 보존 / staging 정리 / review_failed 제외 ─────────────


def test_error_with_only_ignored_noncache_file_is_preserved_intact(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (repo / ".gitignore").write_text("local.db\n.vault.key\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore")
    # 워크트리 HEAD 는 이전 커밋이므로 워크트리에 직접 .gitignore 를 둔다(추적 변경 없이 ignore 만 적용: info/exclude)
    excl = Path(_git(path, "rev-parse", "--git-path", "info/exclude").strip())
    excl = excl if excl.is_absolute() else path / excl
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("local.db\n.vault.key\n")
    (path / "local.db").write_bytes(b"\x00data\x01")
    (path / ".vault.key").write_text("secret\n")

    proc = _immediate(repo, tmp_path, job_id, "error")

    assert path.is_dir()
    assert _result(proc) == "preserved"
    assert "preserved (ignored non-cache)" in proc.stdout and "count=2" in proc.stdout
    assert (path / "local.db").read_bytes() == b"\x00data\x01"
    assert (path / ".vault.key").read_text() == "secret\n"
    assert not (tmp_path / "archive").exists()  # 시크릿을 archive_dir 로 복제하지 않는다


def test_error_with_only_ignored_cache_and_untracked_doc_is_archived_then_removed(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"\x00pyc")
    excl = Path(_git(path, "rev-parse", "--git-path", "info/exclude").strip())
    excl = excl if excl.is_absolute() else path / excl
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("__pycache__/\n*.pyc\n")
    (path / "notes.md").write_text("untracked doc body\n")

    proc = _immediate(repo, tmp_path, job_id, "error")

    assert _result(proc) == "removed", proc.stdout + proc.stderr
    assert not path.exists()
    body = (tmp_path / "archive" / f"{job_id}.dirty-full.patch").read_text()
    assert "untracked doc body" in body and "notes.md" in body


def test_archive_failure_leaves_no_hidden_staging(repo, make_wt, tmp_path, monkeypatch):
    job_id, path = make_wt()
    (path / "big.bin").write_bytes(b"a" * (2 * 1024 * 1024))
    monkeypatch.setenv("RUNNER_WT_ARCHIVE_MAX_PATCH_MB", "1")

    proc = _immediate(repo, tmp_path, job_id, "error")

    assert path.is_dir()
    assert _result(proc) == "preserved"
    arch = tmp_path / "archive"
    assert [n for n in os.listdir(arch) if job_id in n] == []
    assert os.listdir(arch) == []  # 숨김 항목 포함 전체


def test_review_failed_dirty_is_not_archived_or_removed(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    proc = _immediate(repo, tmp_path, job_id, "error", phase="review_failed")

    assert path.is_dir()
    assert (path / "README").read_text() == "edited\n"
    assert _result(proc) == "preserved"
    assert "preserved (not clean)" in proc.stdout
    assert not (tmp_path / "archive").exists()


def test_review_failed_clean_is_still_removed(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    proc = _immediate(repo, tmp_path, job_id, "error", phase="review_failed")
    assert _result(proc) == "removed"
    assert not path.exists()


def test_cancelled_dirty_without_ignored_noncache_is_archived_then_removed(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    proc = _immediate(repo, tmp_path, job_id, "cancelled")
    assert _result(proc) == "removed"
    assert not path.exists()
    assert (tmp_path / "archive" / f"{job_id}.dirty-full.patch").is_file()


def test_runner_hook_passes_phase_and_preserves_dirty_review_failed(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")

    out = harness(f"_reclaim_finished_worktree {job_id}", status="error", phase="review_failed")

    assert path.is_dir()
    assert "result=preserved" in out


# ── R2 재작업: staged index 보존 / phase 조회 실패 fail-closed / error_detail 재작업 대상 ──


def test_staged_only_content_is_archived_before_removal(repo, make_wt, tmp_path):
    job_id, path = make_wt()
    (path / "README").write_text("staged only body\n")
    _git(path, "add", "README")
    orig = _git(path, "show", "HEAD:README")
    (path / "README").write_text(orig)  # 작업 파일은 HEAD 로 복원 — index 에만 변경이 남는다(MM)
    assert "MM" in _git(path, "status", "--porcelain")

    proc = _immediate(repo, tmp_path, job_id, "error", phase="error")

    assert _result(proc) == "removed", proc.stdout + proc.stderr
    assert not path.exists()
    staged = (tmp_path / "archive" / f"{job_id}.staged.patch").read_text()
    assert "staged only body" in staged


def test_unknown_or_missing_phase_never_allows_dirty_removal(repo, make_wt, tmp_path):
    for kw in ({"phase": "__unknown__"}, {"phase_env": False}):
        job_id, path = make_wt()
        (path / "README").write_text("edited\n")
        proc = _immediate(repo, tmp_path, job_id, "error", **kw)
        assert path.is_dir() and _result(proc) == "preserved", kw
        assert (path / "README").read_text() == "edited\n"
    assert not (tmp_path / "archive").exists()


@pytest.mark.parametrize(
    "detail,kept",
    [
        ("deploy_isolated_stale_approval", True),
        ("deploy_isolated_push_state: stale_base origin moved", True),
        ("deploy_isolated_push_state: fetch_fail stale_base", False),
        ("deploy_isolated_push_state: network", False),
        ("some other error", False),
    ],
)
def test_phase_error_auto_rework_detail_keeps_dirty_worktree(repo, make_wt, tmp_path, detail, kept):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    proc = _immediate(repo, tmp_path, job_id, "error", phase="error", detail=detail)
    assert path.is_dir() == kept, proc.stdout
    assert _result(proc) == ("preserved" if kept else "removed")


def test_runner_hook_fails_closed_when_phase_query_fails(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    out = harness(f"_reclaim_finished_worktree {job_id}", status="error", meta_fail=True)
    assert path.is_dir()
    assert "result=preserved" in out


def test_runner_hook_passes_error_detail_for_stale_base_rework(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    out = harness(
        f"_reclaim_finished_worktree {job_id}",
        status="error", phase="error", detail="deploy_isolated_push_state: stale_base",
    )
    assert path.is_dir()
    assert "result=preserved" in out


# ── R3 회귀 테스트: 메타 조회 SQL 오류 / 구분자 불일치 / 미지 phase / error_detail > 1000자 / 동치 ──


def test_runner_hook_preserves_dirty_worktree_on_db_exec_error_string(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    out = harness(
        f"_reclaim_finished_worktree {job_id}",
        status="error",
        meta_raw="ERROR:  relation pipeline_jobs does not exist",
    )
    assert path.is_dir()
    assert "result=preserved" in out


def test_runner_hook_preserves_dirty_worktree_on_delimiter_mismatch(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    out = harness(
        f"_reclaim_finished_worktree {job_id}",
        status="error",
        meta_raw="review_failed",
    )
    assert path.is_dir()
    assert "result=preserved" in out


def test_runner_hook_preserves_dirty_worktree_on_unknown_phase(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    out = harness(
        f"_reclaim_finished_worktree {job_id}",
        status="error",
        meta_raw="unknown_phase_xyz\x1e0",
    )
    assert path.is_dir()
    assert "result=preserved" in out


def test_runner_hook_preserves_dirty_worktree_when_stale_base_beyond_1000_chars(harness, make_wt):
    job_id, path = make_wt()
    (path / "README").write_text("edited\n")
    long_detail = "deploy_isolated_push_state: " + ("padding_" * 200) + " stale_base origin moved"
    assert len(long_detail) > 1000
    assert long_detail.index("stale_base") > 1000
    out = harness(
        f"_reclaim_finished_worktree {job_id}",
        status="error",
        phase="error",
        detail=long_detail,
    )
    assert path.is_dir()
    assert "result=preserved" in out


def test_rework_candidate_sql_matches_python_predicate():
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA case_sensitive_like = ON;")
    cur = con.cursor()
    cur.execute("CREATE TABLE jobs (status TEXT, phase TEXT, error_detail TEXT);")

    statuses = ["error", "done", "cancelled", "running", "failed"]
    phases = [
        "review_failed",
        "push_stale_base",
        "deploy_isolated_stale_approval",
        "deploy_isolated_push_state",
        "error",
        "build_fail",
        "deploy_failed",
        "unknown_phase",
        "",
    ]
    details = [
        "",
        "review_failed: verdict=REQUEST_CHANGES score=0.68",
        "review_failed: verdict=REJECTED score=0.1",
        "review_failed: source=origin_session_adjudicator",
        "deploy_isolated_stale_approval",
        "deploy_isolated_stale_approval with extra stuff",
        "deploy_isolated_push_state: stale_base origin moved",
        "deploy_isolated_push_state: fetch_fail stale_base",
        "deploy_isolated_push_state: network timeout",
        "deploy_isolated_push_state: " + ("x" * 1500) + " stale_base",
        "deploy_isolated_push_state: " + ("x" * 1500) + " stale_base fetch_fail",
        "deploy_isolated_push_state: " + ("x" * 1500),
        "some other error message entirely",
    ]

    for s in statuses:
        for p in phases:
            for d in details:
                py_res = is_request_changes_failure(s, p, d)
                cur.execute("DELETE FROM jobs;")
                cur.execute("INSERT INTO jobs VALUES (?, ?, ?);", (s, p, d))
                sql_res = bool(
                    cur.execute(f"SELECT {REWORK_CANDIDATE_SQL_EXPR} FROM jobs;").fetchone()[0]
                )
                assert py_res == sql_res, (
                    f"Mismatch for status={s!r}, phase={p!r}, detail={d[:40]!r}: "
                    f"py={py_res}, sql={sql_res}"
                )

    runner_code = RUNNER.read_text(encoding="utf-8")
    assert "review_failed: verdict=REQUEST_CHANGES%" in runner_code
    assert "deploy_isolated_stale_approval" in runner_code
    assert "%stale_base%" in runner_code
    assert "%fetch_fail%" in runner_code

