"""Count cap (RUNNER_WT_MAX_KEEP) for scripts/reclaim_runner_worktrees.sh.

The 24h age floor never reclaims anything when worktrees rotate faster than
24h, so a second pass keeps only the newest N reclaimable worktrees.
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

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/reclaim_runner_worktrees.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_worktrees(tmp_path: Path, count: int):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README").write_text("test\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "initial")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    tag = uuid.uuid4().hex[:6]
    # index 0 is the newest; mtime decreases by one minute per index.
    ids = [f"runner-{tag}{i:02d}" for i in range(count)]
    paths = [Path(f"/tmp/aads-wt-{job_id}") for job_id in ids]
    base = time.time() - 600
    for i, path in enumerate(paths):
        _git(repo, "worktree", "add", "--detach", str(path), "HEAD")
        stamp = base - i * 60
        os.utime(path, (stamp, stamp))
        os.utime(path / ".git", (stamp, stamp))
    return repo, ids, paths


@pytest.fixture
def make(tmp_path):
    created: list[Path] = []

    def _factory(count: int):
        repo, ids, paths = _make_worktrees(tmp_path, count)
        created.extend(paths)
        return repo, ids, paths

    yield _factory
    for path in created:
        shutil.rmtree(path, ignore_errors=True)


def _run(tmp_path, repo, ids, statuses, *, max_keep=None, probe="lsof", expect_rc=0, env_file=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("git", "sed", "tail", "date", "find", "timeout", "du", "awk", "mktemp", "rm", "cat", "grep", "mv", "rmdir", "mkdir"):
        if not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(shutil.which(name))
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "docker").chmod(0o755)
    if probe == "fuser":
        (bin_dir / "fuser").write_text('#!/bin/sh\ncase "$1" in */open.txt) exit 0 ;; *) exit 1 ;; esac\n')
        (bin_dir / "fuser").chmod(0o755)
    else:
        (bin_dir / "lsof").write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / "lsof").chmod(0o755)
    rows = "\n".join(f"{ids[i]}|{status}" for i, status in statuses.items())
    (bin_dir / "psql").write_text("#!/bin/sh\ncat <<'ROWS'\n" + rows + "\nROWS\n")
    (bin_dir / "psql").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "RUNNER_WT_MAX_KEEP"} | {
        "PATH": str(bin_dir),
        "RUNNER_WT_REPO": str(repo),
        "RUNNER_WT_RETENTION_HOURS": "24",
        "RUNNER_WT_ENV_FILE": str(env_file or tmp_path / "runner.env"),
        "RUNNER_WT_ARCHIVE_DIR": str(tmp_path / "archive"),
        "DRY_RUN": "0",
    }
    if max_keep is not None:
        env["RUNNER_WT_MAX_KEEP"] = str(max_keep)
    result = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=60, check=False)
    assert result.returncode == expect_rc, result.stderr
    return result


def _over_cap(result) -> int:
    match = re.search(r"reclaimed_over_cap=(\d+)", result.stdout)
    assert match, result.stdout
    return int(match.group(1))


def test_twenty_candidates_keep_newest_twelve(tmp_path, make):
    repo, ids, paths = make(20)
    result = _run(tmp_path, repo, ids, {i: "done" for i in range(20)}, max_keep=12)
    assert _over_cap(result) == 8
    assert [p.exists() for p in paths[:12]] == [True] * 12
    assert [p.exists() for p in paths[12:]] == [False] * 8
    assert "reclaimed=8 " in result.stdout


def test_default_is_twelve(tmp_path, make):
    repo, ids, paths = make(14)
    result = _run(tmp_path, repo, ids, {i: "done" for i in range(14)})
    assert _over_cap(result) == 2
    assert all(p.exists() for p in paths[:12])
    assert not any(p.exists() for p in paths[12:])


def test_rejected_is_preserved_and_not_counted(tmp_path, make):
    repo, ids, paths = make(20)
    statuses = {i: "done" for i in range(20)}
    statuses[18] = statuses[19] = "rejected"
    result = _run(tmp_path, repo, ids, statuses, max_keep=12)
    assert paths[18].is_dir() and paths[19].is_dir()
    # 18 reclaimable candidates -> newest 12 stay, oldest 6 go.
    assert _over_cap(result) == 6
    assert all(p.exists() for p in paths[:12])
    assert not any(p.exists() for p in paths[12:18])


def test_unknown_status_is_preserved(tmp_path, make):
    repo, ids, paths = make(14)
    statuses = {i: "done" for i in range(12)}
    result = _run(tmp_path, repo, ids, statuses, max_keep=12)
    assert _over_cap(result) == 0
    assert all(p.is_dir() for p in paths)


def test_active_process_worktree_is_preserved_and_not_counted(tmp_path, make):
    repo, ids, paths = make(15)
    for index in (13, 14):
        (paths[index] / "open.txt").write_text("busy\n")
    result = _run(tmp_path, repo, ids, {i: "done" for i in range(15)}, max_keep=12, probe="fuser")
    assert paths[13].is_dir() and paths[14].is_dir()
    # 13 non-busy candidates -> only the oldest one goes.
    assert _over_cap(result) == 1
    assert not paths[12].exists()
    assert all(p.exists() for p in paths[:12])


def test_max_keep_zero_disables_cap(tmp_path, make):
    repo, ids, paths = make(20)
    result = _run(tmp_path, repo, ids, {i: "done" for i in range(20)}, max_keep=0)
    assert _over_cap(result) == 0
    assert "reclaimed=0 " in result.stdout
    assert all(p.is_dir() for p in paths)


def test_env_file_zero_disables_cap(tmp_path, make):
    repo, ids, paths = make(14)
    env_file = tmp_path / "custom.env"
    env_file.write_text("RUNNER_WT_MAX_KEEP=0\n")
    result = _run(tmp_path, repo, ids, {i: "done" for i in range(14)}, env_file=env_file)
    assert _over_cap(result) == 0
    assert all(p.is_dir() for p in paths)


@pytest.mark.parametrize("value", ["abc", "-1", "1.5"])
def test_invalid_max_keep_exits_2(tmp_path, make, value):
    repo, ids, _ = make(1)
    result = _run(tmp_path, repo, ids, {}, max_keep=value, expect_rc=2)
    assert "invalid RUNNER_WT_MAX_KEEP" in result.stderr
