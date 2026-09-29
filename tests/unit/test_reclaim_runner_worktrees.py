"""Runner worktree reclamation with a real temporary Git repository."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/reclaim_runner_worktrees.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def worktrees(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README").write_text("test\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "initial")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    ids = {name: f"runner-{uuid.uuid4().hex[:8]}" for name in ("recent", "old", "deploy", "active", "nested")}
    paths = {name: Path(f"/tmp/aads-wt-{job_id}") for name, job_id in ids.items()}
    for path in paths.values():
        _git(repo, "worktree", "add", "--detach", str(path), "HEAD")
    try:
        yield repo, paths, ids, tmp_path
    finally:
        for path in paths.values():
            shutil.rmtree(path, ignore_errors=True)


def _age(path: Path, hours: int = 25) -> None:
    old = time.time() - hours * 3600
    for root, dirs, files in os.walk(path):
        for name in files:
            os.utime(Path(root) / name, (old, old))
        for name in dirs:
            os.utime(Path(root) / name, (old, old))
    os.utime(path, (old, old))


def _run(worktrees, *, dry_run=False, db_ok=True, statuses=None, extra_env=None, probe_mode="lsof"):
    repo, paths, ids, tmp_path = worktrees
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("git", "sed", "tail", "date", "find", "timeout", "du", "awk", "mktemp", "rm", "cat", "grep", "mv", "rmdir", "mkdir"):
        if not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(shutil.which(name))
    for name in (("lsof",) if probe_mode == "lsof" else ("fuser",) if probe_mode == "fuser" else ()) + ("docker",):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
    if probe_mode == "fuser":
        (bin_dir / "fuser").write_text('#!/bin/sh\ncase "$1" in */open.txt) exit 0 ;; *) exit 1 ;; esac\n')
    rows = "\n".join(f"{ids[name]}|{status}" for name, status in (statuses or {}).items())
    psql = bin_dir / "psql"
    body = "exit 1" if not db_ok else "cat <<'ROWS'\n" + rows + "\nROWS"
    psql.write_text("#!/bin/sh\n" + body + "\n")
    psql.chmod(0o755)
    env = os.environ | {
        "PATH": str(bin_dir),
        "RUNNER_WT_REPO": str(repo),
        "RUNNER_WT_RETENTION_HOURS": "24",
        "DRY_RUN": "1" if dry_run else "0",
        "COMPOSE_DIR": str(paths["deploy"]),
        "RUNNER_WT_ENV_FILE": str(tmp_path / "runner.env"),
        "RUNNER_WT_ARCHIVE_DIR": str(tmp_path / "archive"),
    }
    env.update(extra_env or {})
    result = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return result


def test_recent_old_main_and_deploy_exclusion(worktrees):
    repo, paths, _, _ = worktrees
    for name in ("old", "deploy", "active"):
        _age(paths[name])
    result = _run(worktrees, statuses={"old": "done", "active": "awaiting_approval"})
    assert repo.is_dir() and paths["recent"].is_dir() and paths["deploy"].is_dir()
    assert not paths["old"].exists()
    assert paths["active"].is_dir()
    assert "skipped_active=2" in result.stdout


def test_dry_run_lists_candidate_and_removes_nothing(worktrees):
    _, paths, _, _ = worktrees
    _age(paths["old"])
    result = _run(worktrees, dry_run=True, statuses={"old": "done"})
    assert all(path.is_dir() for path in paths.values())
    assert f"DRY_RUN candidate={paths['old']}" in result.stdout
    assert "reclaimed=0" in result.stdout


def test_db_outage_preserves_unknown_and_nonterminal_status(worktrees):
    _, paths, _, _ = worktrees
    _age(paths["old"])
    assert paths["old"].is_dir()
    _run(worktrees, statuses={"old": "reviewing"})
    assert paths["old"].is_dir()
    _run(worktrees, statuses={})
    assert paths["old"].is_dir()
    result = _run(worktrees, db_ok=False)
    assert paths["old"].is_dir()
    assert "reclaimed=0" in result.stdout
    assert "skipped_unknown=1" in result.stdout
    assert "DB unavailable" in result.stderr


def test_db_outage_override_requires_extended_retention(worktrees):
    _, paths, _, _ = worktrees
    _age(paths["old"], hours=25)
    result = _run(worktrees, db_ok=False, extra_env={"RUNNER_WT_ALLOW_NO_DB": "1"})
    assert paths["old"].is_dir()
    assert "skipped_unknown=1" in result.stdout
    _age(paths["old"], hours=169)
    result = _run(worktrees, db_ok=False, extra_env={"RUNNER_WT_ALLOW_NO_DB": "1"})
    assert not paths["old"].exists()
    assert "reclaimed=1" in result.stdout


def test_missing_status_uses_unknown_rule(worktrees):
    _, paths, _, _ = worktrees
    _age(paths["old"], hours=169)
    result = _run(worktrees, statuses={})
    assert paths["old"].is_dir()
    assert "skipped_unknown=1" in result.stdout


def test_unmerged_commit_is_bundled_before_reclaim(worktrees):
    repo, paths, ids, tmp_path = worktrees
    (paths["old"] / "evidence.txt").write_text("review evidence\n")
    _git(paths["old"], "add", "evidence.txt")
    _git(paths["old"], "commit", "-qm", "review evidence")
    head = subprocess.check_output(["git", "-C", str(paths["old"]), "rev-parse", "HEAD"], text=True).strip()
    _age(paths["old"])
    result = _run(worktrees, statuses={"old": "error"})
    assert not paths["old"].exists()
    assert "reclaimed=1" in result.stdout
    bundle = tmp_path / "archive" / f"{ids['old']}.bundle"
    assert bundle.is_file()
    assert head in subprocess.check_output(["git", "bundle", "list-heads", str(bundle)], text=True, cwd=str(repo))


def test_bundle_verification_failure_preserves_worktree(worktrees):
    _, paths, _, tmp_path = worktrees
    (paths["old"] / "evidence.txt").write_text("evidence\n")
    _git(paths["old"], "add", "evidence.txt")
    _git(paths["old"], "commit", "-qm", "evidence")
    _age(paths["old"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_git = shutil.which("git")
    wrapper = bin_dir / "git"
    wrapper.write_text(f'#!/bin/sh\ncase " $* " in *" bundle verify "*) exit 1 ;; esac\nexec "{real_git}" "$@"\n')
    wrapper.chmod(0o755)
    result = _run(worktrees, statuses={"old": "error"})
    assert paths["old"].is_dir()
    assert "skipped_archive_failed=1" in result.stdout


def test_worktree_from_other_repository_is_reclaimed(worktrees):
    _, paths, ids, tmp_path = worktrees
    other = tmp_path / "other_repo"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.name", "Test")
    _git(other, "config", "user.email", "test@example.com")
    (other / "README").write_text("other\n")
    _git(other, "add", "README")
    _git(other, "commit", "-qm", "initial")
    _git(other, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(paths["old"], "worktree", "remove", "--force", str(paths["old"]))
    _git(other, "worktree", "add", "--detach", str(paths["old"]), "HEAD")
    _age(paths["old"])
    result = _run(worktrees, statuses={"old": "done"})
    assert not paths["old"].exists()
    assert "reclaimed=1" in result.stdout


def test_nested_file_edit_extends_retention(worktrees):
    _, paths, _, _ = worktrees
    nested = paths["nested"] / "sub"
    nested.mkdir()
    changed = nested / "changed"
    changed.write_text("before")
    _age(paths["nested"])
    changed.write_text("after")
    result = _run(worktrees, statuses={"nested": "done"})
    assert paths["nested"].is_dir()
    assert "skipped_recent=" in result.stdout


def test_summary_is_last_line_and_parseable(worktrees):
    result = _run(worktrees)
    assert re.fullmatch(
        r"\[reclaim-wt\] reclaimed=\d+ freed=\d+MB archived=\d+ skipped_active=\d+ skipped_recent=\d+ skipped_unknown=\d+ skipped_unknown_repo=\d+ skipped_archive_failed=\d+",
        result.stdout.splitlines()[-1],
    )


def test_dirty_terminal_worktree_is_patched_then_reclaimed(worktrees):
    _, paths, ids, tmp_path = worktrees
    (paths["old"] / "README").write_text("changed\n")
    (paths["old"] / "untracked.txt").write_text("recover me")
    _age(paths["old"])
    result = _run(worktrees, statuses={"old": "error"})
    assert not paths["old"].exists()
    archive = tmp_path / "archive"
    assert "+changed" in (archive / f"{ids['old']}.dirty.patch").read_text()
    assert "untracked.txt" in (archive / f"{ids['old']}.untracked.list").read_text()
    assert "archived=1" in result.stdout


def test_ignored_artifact_is_listed_then_reclaimed(worktrees):
    _, paths, ids, tmp_path = worktrees
    (paths["old"] / ".gitignore").write_text("artifact.log\n")
    _git(paths["old"], "add", ".gitignore")
    _git(paths["old"], "commit", "-qm", "ignore artifact")
    head = subprocess.check_output(
        ["git", "-C", str(paths["old"]), "rev-parse", "HEAD"], text=True
    ).strip()
    _git(worktrees[0], "update-ref", "refs/remotes/origin/main", head)
    (paths["old"] / "artifact.log").write_text("recover me")
    _age(paths["old"])
    result = _run(worktrees, statuses={"old": "done"})
    assert not paths["old"].exists()
    assert "artifact.log" in (tmp_path / "archive" / f"{ids['old']}.ignored.list").read_text()
    assert "reclaimed=1" in result.stdout


def test_locked_worktree_is_not_removed_by_filesystem_fallback(worktrees):
    repo, paths, _, _ = worktrees
    _git(repo, "worktree", "lock", str(paths["old"]))
    _age(paths["old"])
    result = _run(worktrees, statuses={"old": "done"})
    assert paths["old"].is_dir()
    assert "removal failed" in result.stderr
    assert "reclaimed=0" in result.stdout


def test_runner_environment_extends_retention(worktrees):
    _, paths, _, tmp_path = worktrees
    (tmp_path / "runner.env").write_text("ARTIFACT_MAX_AGE_HOURS=72\n")
    _age(paths["old"], hours=48)
    result = _run(worktrees, statuses={"old": "done"})
    assert paths["old"].is_dir()
    assert "skipped_recent=" in result.stdout


def test_fuser_checks_nested_open_files(worktrees):
    _, paths, _, _ = worktrees
    nested = paths["old"] / "sub"
    nested.mkdir()
    (nested / "open.txt").write_text("tracked later")
    _git(paths["old"], "add", "sub/open.txt")
    _git(paths["old"], "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "file")
    committed_head = subprocess.check_output(["git", "-C", str(paths["old"]), "rev-parse", "HEAD"], text=True).strip()
    _git(worktrees[0], "update-ref", "refs/remotes/origin/main", committed_head)
    _age(paths["old"])
    result = _run(worktrees, statuses={"old": "done"}, probe_mode="fuser")
    assert paths["old"].is_dir()
    assert "skipped_active=" in result.stdout


def test_missing_process_tools_preserve_candidate(worktrees):
    _, paths, _, _ = worktrees
    _age(paths["old"])
    result = _run(worktrees, statuses={"old": "done"}, probe_mode="none")
    assert paths["old"].is_dir()
    assert "process check unavailable" in result.stderr
