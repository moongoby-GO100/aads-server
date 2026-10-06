"""Execute real runner functions against local Git repositories and live cwd PIDs.

Only DB/chat/log side effects are replaced. Git, /proc ownership, worktree removal,
staging and commits are real; no production runner or network service is started.
"""
import os
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNERS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def extract(source, name):
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


@pytest.fixture(params=RUNNERS)
def runner(request, tmp_path):
    source = (ROOT / "scripts" / request.param).read_text()
    names = ("sql_escape", "is_deploy_only_instruction", "is_git_workdir",
             "git_dirty_count", "verify_isolated_job_worktree", "job_was_requeued",
             "requeue_scope_violations", "verify_worker_commit_provenance",
             "commit_job_worktree_for_approval", "worktree_busy_pids",
             "prepare_clean_job_worktree")
    script = tmp_path / "functions.sh"
    script.write_text("set -eo pipefail\n" + "\n".join(
        extract(source, n) for n in names if f"{n}() {{" in source
    ) + r'''
log() { printf '%s\n' "$*" >> "$QA_LOG"; }
_fail_job() { printf '%s\n' "$3" >> "$QA_FAILURE"; }
db_exec() { printf '%s' "${QA_REQUEUED:-1}"; }
db_update() { printf '%s\n' "$1" >> "$QA_SQL"; }
record_runner_event() { :; }
''')
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_ALLOW_PROTOCOL": "file", "QA_LOG": str(tmp_path / "log"),
           "QA_FAILURE": str(tmp_path / "failure"), "QA_SQL": str(tmp_path / "sql")}

    def invoke(name, *args, requeued=True):
        return subprocess.run(["bash", "-c", 'source "$1"; shift; "$@"',
                               "qa", str(script), name, *map(str, args)],
                              cwd=tmp_path, env={**env, "QA_REQUEUED": str(int(requeued))},
                              text=True, capture_output=True, timeout=15)
    invoke.env = env
    invoke.source = source
    invoke.failure = tmp_path / "failure"
    invoke.sql = tmp_path / "sql"
    return invoke


@pytest.fixture
def repo(tmp_path, runner):
    main = tmp_path / "main"
    origin = tmp_path / "origin.git"
    jid = "runner-integrity-test-" + uuid.uuid4().hex
    worktree = Path("/tmp") / ("aads-wt-" + jid)

    def git(path, *args):
        return subprocess.run(["git", "-C", str(path), *args], env=runner.env,
                              text=True, capture_output=True, check=True, timeout=10).stdout.strip()
    main.mkdir()
    git(main, "init", "-b", "main")
    git(main, "config", "user.email", "qa@example.invalid")
    git(main, "config", "user.name", "Runner QA")
    (main / "tracked.txt").write_text("baseline\n")
    git(main, "add", ".")
    git(main, "commit", "-m", "baseline")
    subprocess.run(["git", "init", "--bare", str(origin)], env=runner.env,
                   check=True, capture_output=True)
    git(main, "remote", "add", "origin", str(origin))
    git(main, "push", "origin", "main")
    base = git(main, "rev-parse", "HEAD")
    git(main, "worktree", "add", "--detach", str(worktree), base)
    yield {"main": main, "wt": worktree, "base": base, "git": git, "jid": jid}
    if worktree.exists():
        git(main, "worktree", "remove", "--force", str(worktree))
    else:
        git(main, "worktree", "prune")


def test_live_cwd_preserves_worktree_inode_head_and_dirty_files(runner, repo):
    w = repo["wt"]
    (w / "tracked.txt").write_text("worker edits\n")
    (w / "untracked.txt").write_text("worker evidence\n")
    inode = w.stat().st_ino
    status = repo["git"](w, "status", "--porcelain")
    worker = subprocess.Popen(["sleep", "30"], cwd=w)
    try:
        result = runner("prepare_clean_job_worktree", repo["jid"], "GO100", "qa", repo["main"], w)
        assert result.returncode != 0, result.stderr
        assert "worktree_path_busy" in runner.failure.read_text()
        assert worker.poll() is None
        assert w.stat().st_ino == inode
        assert repo["git"](w, "rev-parse", "HEAD") == repo["base"]
        assert repo["git"](w, "status", "--porcelain") == status
        assert (w / "tracked.txt").read_text() == "worker edits\n"
        assert (w / "untracked.txt").read_text() == "worker evidence\n"
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_idle_worktree_can_be_prepared_from_local_origin(runner, repo):
    result = runner("prepare_clean_job_worktree", repo["jid"], "GO100", "qa", repo["main"], repo["wt"])
    assert result.returncode == 0, result.stderr
    assert repo["git"](repo["wt"], "rev-parse", "HEAD") == repo["base"]
    assert not repo["git"](repo["wt"], "status", "--porcelain")


@pytest.mark.parametrize("state", ["untracked", "staged", "committed"])
def test_unrelated_added_file_is_rejected_for_requeued_job(runner, repo, state):
    w = repo["wt"]
    (w / "unrelated.py").write_text("unrelated = True\n")
    if state != "untracked":
        repo["git"](w, "add", ".")
    if state == "committed":
        repo["git"](w, "commit", "-m", "unrelated addition")
    result = runner("requeue_scope_violations", repo["jid"], w, "Edit tracked.txt only", repo["base"])
    assert result.returncode != 0
    assert result.stdout.strip() == "unrelated.py"
    assert (w / "unrelated.py").exists()


@pytest.mark.parametrize("state", ["untracked", "staged", "committed"])
def test_explicit_allowed_added_file_remains_permitted(runner, repo, state):
    w = repo["wt"]
    (w / "allowed.py").write_text("allowed = True\n")
    if state != "untracked":
        repo["git"](w, "add", ".")
    if state == "committed":
        repo["git"](w, "commit", "-m", "allowed addition")
    result = runner("requeue_scope_violations", repo["jid"], w, "Create allowed.py", repo["base"])
    assert result.returncode == 0, result.stdout + result.stderr


def test_recreated_main_head_cannot_be_adopted_as_job_commit(runner, repo):
    # The old attempt's base remains A while its path is now clean at main B.
    main, w, git = repo["main"], repo["wt"], repo["git"]
    (main / "capacity_registry.py").write_text("unrelated_main_change = True\n")
    git(main, "add", ".")
    git(main, "commit", "-m", "another job: DR02")
    git(main, "push", "origin", "main")
    new_main = git(main, "rev-parse", "HEAD")
    git(w, "reset", "--hard", new_main)
    result = runner("commit_job_worktree_for_approval", repo["jid"], "qa", w, main,
                    "Implement another feature", repo["base"], requeued=False)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "approval_commit_provenance_mismatch" in runner.failure.read_text()
    assert not runner.sql.exists()
    assert git(w, "rev-parse", "HEAD") == new_main
    assert not git(w, "status", "--porcelain")


def test_actual_worker_commit_is_adopted(runner, repo):
    w, git = repo["wt"], repo["git"]
    (w / "allowed.py").write_text("worker_change = True\n")
    git(w, "add", ".")
    git(w, "commit", "-m", "worker implementation")
    expected = git(w, "rev-parse", "HEAD")
    result = runner("commit_job_worktree_for_approval", repo["jid"], "qa", w, repo["main"],
                    "Create allowed.py", repo["base"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == expected
    assert "commit_hash=" in runner.sql.read_text()


def test_new_worker_changes_are_committed(runner, repo):
    w = repo["wt"]
    (w / "allowed.py").write_text("worker_change = True\n")
    result = runner("commit_job_worktree_for_approval", repo["jid"], "qa", w, repo["main"],
                    "Create allowed.py", repo["base"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout != repo["base"]
    assert repo["git"](w, "rev-parse", "HEAD") == result.stdout
    assert not repo["git"](w, "status", "--porcelain")


def test_failed_prepare_or_approval_caller_preserves_existing_worker_output(runner):
    # run_job cannot be executed in a unit test: it claims jobs and calls the model.
    # Enforce that this failure branch preserves the previous attempt's .out/.err.
    source = runner.source
    start = source.index('prepare_clean_job_worktree "$job_id" "$project"')
    end = source.index('workdir="$worktree_dir"', start)
    assert "_cleanup_artifacts" not in source[start:end]
    start = source.index('approval_commit_sha=$(commit_job_worktree_for_approval "$job_id"')
    end = source.index('    # 방어:', start)
    assert "_cleanup_artifacts" not in source[start:end]


def test_two_runner_copies_keep_integrity_helpers_identical():
    sources = [(ROOT / "scripts" / n).read_text() for n in RUNNERS]
    for name in ("requeue_scope_violations", "verify_worker_commit_provenance",
                 "commit_job_worktree_for_approval", "worktree_busy_pids",
                 "prepare_clean_job_worktree"):
        assert extract(sources[0], name) == extract(sources[1], name)


def test_fresh_worktree_can_be_created(runner, repo):
    repo["git"](repo["main"], "worktree", "remove", str(repo["wt"]))
    result = runner("prepare_clean_job_worktree", repo["jid"], "GO100", "qa", repo["main"], repo["wt"])
    assert result.returncode == 0, result.stderr
    assert repo["git"](repo["wt"], "rev-parse", "HEAD") == repo["base"]


def test_invalid_base_fails_without_staging_or_changing_worker_files(runner, repo):
    w = repo["wt"]
    (w / "allowed.py").write_text("preserve_me = True\n")
    before = repo["git"](w, "status", "--porcelain")
    result = runner("commit_job_worktree_for_approval", repo["jid"], "qa", w, repo["main"],
                    "Create allowed.py", "f" * 40)
    assert result.returncode != 0
    assert "approval_commit_provenance_mismatch" in runner.failure.read_text()
    assert repo["git"](w, "status", "--porcelain") == before
    assert not runner.sql.exists()


def test_deploy_only_current_base_remains_supported(runner, repo):
    result = runner("commit_job_worktree_for_approval", repo["jid"], "qa", repo["wt"], repo["main"],
                    "DEPLOY_ONLY: true", repo["base"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == repo["base"]


@pytest.mark.parametrize("state", ["root_absent", "root_recreated", "child_root_absent"])
def test_deleted_cwd_blocks_reuse_even_if_original_path_is_absent(runner, repo, state):
    w, git, main = repo["wt"], repo["git"], repo["main"]
    cwd = w / "nested" / "worker" if state == "child_root_absent" else w
    cwd.mkdir(parents=True, exist_ok=True)
    worker = subprocess.Popen(["sleep", "30"], cwd=cwd)
    try:
        git(main, "worktree", "remove", "--force", str(w))
        deleted_cwd = os.readlink(f"/proc/{worker.pid}/cwd")
        assert deleted_cwd == str(cwd) + " (deleted)"
        if state == "root_recreated":
            git(main, "worktree", "add", "--detach", str(w), repo["base"])
            (w / "tracked.txt").write_text("new path edits must survive\n")
            (w / "new_evidence.txt").write_text("new path evidence must survive\n")
            preserved = (w.stat().st_ino, git(w, "rev-parse", "HEAD"), git(w, "status", "--porcelain"))
        else:
            assert not w.exists()
        main_head = git(main, "rev-parse", "HEAD")
        origin_head = git(main, "rev-parse", "origin/main")

        result = runner("prepare_clean_job_worktree", repo["jid"], "GO100", "qa", main, w)

        assert result.returncode != 0, result.stdout + result.stderr
        assert "worktree_path_busy" in runner.failure.read_text()
        assert worker.poll() is None
        assert os.readlink(f"/proc/{worker.pid}/cwd") == deleted_cwd
        assert git(main, "rev-parse", "HEAD") == main_head
        assert git(main, "rev-parse", "origin/main") == origin_head
        if state == "root_recreated":
            assert (w.stat().st_ino, git(w, "rev-parse", "HEAD"), git(w, "status", "--porcelain")) == preserved
            assert (w / "tracked.txt").read_text() == "new path edits must survive\n"
            assert (w / "new_evidence.txt").read_text() == "new path evidence must survive\n"
        else:
            assert not w.exists(), "busy deleted cwd must prevent a new worktree at its old path"
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_deleted_sibling_prefix_does_not_block_an_unrelated_worktree(runner, repo):
    # /tmp/job-other is not /tmp/job or a child of /tmp/job.
    sibling = repo["wt"].with_name(repo["wt"].name + "-other")
    sibling.mkdir()
    worker = subprocess.Popen(["sleep", "30"], cwd=sibling)
    try:
        sibling.rmdir()
        assert os.readlink(f"/proc/{worker.pid}/cwd") == str(sibling) + " (deleted)"
        result = runner("prepare_clean_job_worktree", repo["jid"], "GO100", "qa", repo["main"], repo["wt"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert worker.poll() is None
        assert repo["git"](repo["wt"], "rev-parse", "HEAD") == repo["base"]
    finally:
        worker.terminate()
        worker.wait(timeout=5)
