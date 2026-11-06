"""scripts/runtime_release_refresh.sh — 진짜 git/bash/flock 으로 임시 저장소에서 검증한다.

운영 폴더(/root/aads/aads-server)와 실제 릴리스 사본은 쓰지 않는다. 경로는 모두 환경변수로 주입한다.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/runtime_release_refresh.sh"
SERVICE = ROOT / "deploy/systemd/aads-runtime-release-refresh.service"
TIMER = ROOT / "deploy/systemd/aads-runtime-release-refresh.timer"
CHECKER = ROOT / "scripts/check_runtime_exec_paths.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd, *args):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "protocol.file.allow=always", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()


def commit(repo, name, text):
    (Path(repo) / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"{name}:{text}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    dev = tmp_path / "dev"
    ops = tmp_path / "ops"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    git(dev, "checkout", "-q", "-b", "main")
    c1 = commit(dev, "a.txt", "one\n")
    c2 = commit(dev, "b.txt", "two\n")
    git(dev, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(ops))
    c3 = commit(dev, "c.txt", "three\n")
    git(dev, "push", "-q", "origin", "main")
    w = {
        "tmp": tmp_path, "origin": origin, "dev": dev, "ops": ops,
        "wt": tmp_path / "runtime" / "release",
        "lock": tmp_path / "lock" / "release.lock",
        "log": tmp_path / "log" / "release.log",
        "state": tmp_path / "state" / "release.state",
        "c1": c1, "c2": c2, "c3": c3,
    }
    return w


def run(w, sha=None, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNTIME_RELEASE_")}
    env.update(GIT_ENV)
    env.update({
        "RUNTIME_RELEASE_WT": str(w["wt"]),
        "RUNTIME_RELEASE_REPO": str(w["ops"]),
        "RUNTIME_RELEASE_LOCK": str(w["lock"]),
        "RUNTIME_RELEASE_LOG": str(w["log"]),
        "RUNTIME_RELEASE_STATE": str(w["state"]),
        "RUNTIME_RELEASE_FETCH_TIMEOUT": "20",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always",
    })
    if sha:
        env["RUNTIME_RELEASE_SHA"] = sha
    env.update({k: str(v) for k, v in extra.items()})
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)


def log_text(w):
    return w["log"].read_text() if w["log"].exists() else ""


def state(w):
    return dict(line.split("=", 1) for line in w["state"].read_text().splitlines())


def tree_digest(path):
    h = hashlib.sha256()
    for p in sorted(Path(path).rglob("*")):
        if ".git" in p.relative_to(path).parts[:1] and p.is_dir():
            continue
        rel = str(p.relative_to(path))
        if p.is_file() and not p.is_symlink():
            if rel.startswith(".git/") and (rel.startswith(".git/worktrees") or rel in (".git/FETCH_HEAD",)):
                continue
            if rel.startswith(".git/refs/remotes") or rel.startswith(".git/logs") or rel.startswith(".git/objects") \
                    or rel.startswith(".git/packed-refs"):
                continue
            h.update(rel.encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def ops_snapshot(ops):
    return {
        "head": git(ops, "rev-parse", "HEAD"),
        "branch": subprocess.run(["git", "-C", str(ops), "symbolic-ref", "-q", "HEAD"],
                                 capture_output=True, text=True).stdout.strip(),
        "status": git(ops, "status", "--porcelain=v1", "-uall"),
        "index": (Path(git(ops, "rev-parse", "--absolute-git-dir")) / "index").read_bytes(),
        "files": tree_digest(ops),
    }


def test_script_syntax_and_executable():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


def test_creates_missing_worktree_at_origin_main(world):
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(world["wt"], "rev-parse", "HEAD") == world["c3"]
    detached = subprocess.run(["git", "-C", str(world["wt"]), "symbolic-ref", "-q", "HEAD"], capture_output=True)
    assert detached.returncode != 0
    assert state(world)["sha"] == world["c3"]
    assert state(world)["updated_at"]
    assert f"REFRESHED prev=none new={world['c3']}" in log_text(world)
    assert str(world["wt"]) in git(world["ops"], "worktree", "list")


def test_refreshes_clean_worktree_to_new_origin_main(world):
    assert run(world, world["c1"]).returncode == 0
    assert git(world["wt"], "rev-parse", "HEAD") == world["c1"]
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(world["wt"], "rev-parse", "HEAD") == world["c3"]
    assert (world["wt"] / "c.txt").read_text() == "three\n"
    assert f"REFRESHED prev={world['c1']} new={world['c3']}" in log_text(world)
    assert state(world)["sha"] == world["c3"]


def test_same_sha_is_idempotent(world):
    assert run(world).returncode == 0
    log_before = log_text(world)
    state_before = world["state"].read_bytes()
    head_before = git(world["wt"], "rev-parse", "HEAD")
    r = run(world)
    assert r.returncode == 0
    assert log_text(world) == log_before
    assert world["state"].read_bytes() == state_before
    assert git(world["wt"], "rev-parse", "HEAD") == head_before


def test_same_sha_with_dirty_worktree_changes_nothing(world):
    assert run(world).returncode == 0
    (world["wt"] / "a.txt").write_text("local edit\n")
    log_before = log_text(world)
    r = run(world)
    assert r.returncode == 0
    assert log_text(world) == log_before
    assert (world["wt"] / "a.txt").read_text() == "local edit\n"


def test_missing_state_file_is_rebuilt_without_touching_worktree(world):
    assert run(world).returncode == 0
    world["state"].unlink()
    assert run(world).returncode == 0
    assert state(world)["sha"] == world["c3"]
    assert "STATE_SYNC" in log_text(world)


@pytest.mark.parametrize("kind", ["modified", "untracked", "staged"])
def test_dirty_worktree_blocks_refresh_and_stays_untouched(world, kind):
    assert run(world, world["c1"]).returncode == 0
    wt = world["wt"]
    if kind == "modified":
        (wt / "a.txt").write_text("edited\n")
    elif kind == "untracked":
        (wt / "scratch.txt").write_text("x\n")
    else:
        (wt / "a.txt").write_text("staged\n")
        git(wt, "add", "a.txt")
    status_before = git(wt, "status", "--porcelain=v1")
    digest_before = tree_digest(wt)
    state_before = world["state"].read_bytes()

    r = run(world)
    assert r.returncode == 3, r.stdout + r.stderr
    assert re.search(r"REFRESH_BLOCKED dirty=1\b", log_text(world))
    assert git(wt, "rev-parse", "HEAD") == world["c1"]
    assert git(wt, "status", "--porcelain=v1") == status_before
    assert tree_digest(wt) == digest_before
    assert world["state"].read_bytes() == state_before


def test_unpushed_sha_is_rejected_and_worktree_untouched(world):
    assert run(world, world["c1"]).returncode == 0
    local_only = commit(world["ops"], "local.txt", "never pushed\n")
    r = run(world, local_only)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "SHA_REJECTED reason=not_pushed" in log_text(world)
    assert git(world["wt"], "rev-parse", "HEAD") == world["c1"]
    assert state(world)["sha"] == world["c1"]


def test_unpushed_sha_does_not_create_worktree(world):
    local_only = commit(world["ops"], "local.txt", "never pushed\n")
    r = run(world, local_only)
    assert r.returncode == 2
    assert not world["wt"].exists()


def test_unknown_and_malformed_sha_are_rejected(world):
    assert run(world, "0123456789abcdef0123456789abcdef01234567").returncode == 2
    assert "reason=unknown" in log_text(world)
    assert run(world, "main; rm -rf /").returncode == 2
    assert "reason=bad_format" in log_text(world)
    assert run(world, "--detach").returncode == 2
    assert not world["wt"].exists()


def test_explicit_pushed_older_sha_is_allowed_for_rollback(world):
    assert run(world).returncode == 0
    r = run(world, world["c2"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(world["wt"], "rev-parse", "HEAD") == world["c2"]
    assert not (world["wt"] / "c.txt").exists()


def test_abbreviated_pushed_sha_resolves_to_full(world):
    r = run(world, world["c2"][:10])
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(world["wt"], "rev-parse", "HEAD") == world["c2"]
    assert state(world)["sha"] == world["c2"]


def test_ops_folder_state_is_byte_identical_across_all_operations(world):
    ops = world["ops"]
    (ops / "a.txt").write_text("ops local edit\n")
    (ops / "untracked.env").write_text("SECRET=1\n")
    before = ops_snapshot(ops)

    assert run(world).returncode == 0                      # 생성
    assert run(world, world["c1"]).returncode == 0         # 롤백 갱신
    assert run(world).returncode == 0                      # 앞으로 갱신
    (world["wt"] / "scratch").write_text("x")
    assert run(world, world["c1"]).returncode == 3         # dirty 차단
    assert run(world, "deadbeef").returncode == 2          # 거부

    assert ops_snapshot(ops) == before
    assert git(ops, "rev-parse", "HEAD") == world["c2"]


def test_lock_held_defers_without_touching_anything(world):
    world["lock"].parent.mkdir(parents=True, exist_ok=True)
    with open(world["lock"], "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DEFER" in log_text(world)
    assert not world["wt"].exists()
    assert not world["state"].exists()
    # 잠금이 풀리면 정상 진행
    assert run(world).returncode == 0
    assert git(world["wt"], "rev-parse", "HEAD") == world["c3"]


def test_lock_held_defers_existing_worktree_update(world):
    assert run(world, world["c1"]).returncode == 0
    with open(world["lock"], "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = run(world)
    assert r.returncode == 0
    assert git(world["wt"], "rev-parse", "HEAD") == world["c1"]


def test_worktree_path_equal_to_ops_folder_is_refused(world):
    before = ops_snapshot(world["ops"])
    r = run(world, RUNTIME_RELEASE_WT=world["ops"])
    assert r.returncode == 5, r.stdout + r.stderr
    assert "UNSAFE_WT" in log_text(world)
    assert ops_snapshot(world["ops"]) == before


def test_subdirectory_of_ops_folder_is_refused(world):
    sub = world["ops"] / "subdir"
    sub.mkdir()
    (sub / "f").write_text("x")
    before = ops_snapshot(world["ops"])
    r = run(world, RUNTIME_RELEASE_WT=sub)
    assert r.returncode == 5
    assert ops_snapshot(world["ops"]) == before


def test_worktree_of_a_different_repository_is_refused(world):
    other = world["tmp"] / "other_repo"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    head = commit(other, "x", "x")
    r = run(world, RUNTIME_RELEASE_WT=other)
    assert r.returncode == 5
    assert git(other, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("rel", ["nested-release", "deep/er/nested-release", ".git/nested-release"])
def test_new_worktree_inside_ops_folder_is_refused_and_ops_untouched(world, rel):
    before = ops_snapshot(world["ops"])
    status_before = git(world["ops"], "status", "--porcelain", "-uall")
    r = run(world, RUNTIME_RELEASE_WT=world["ops"] / rel)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "UNSAFE_WT" in log_text(world)
    assert not (world["ops"] / rel).exists()
    assert git(world["ops"], "status", "--porcelain", "-uall") == status_before
    assert ops_snapshot(world["ops"]) == before
    assert git(world["ops"], "worktree", "list", "--porcelain").count("worktree ") == 1


def test_new_worktree_inside_ops_folder_via_dotdot_is_refused(world):
    r = run(world, RUNTIME_RELEASE_WT=f"{world['tmp']}/runtime/../ops/sneaky/release")
    assert r.returncode == 5, r.stdout + r.stderr
    assert not (world["ops"] / "sneaky").exists()


def test_new_worktree_inside_ops_folder_via_symlinked_parent_is_refused(world):
    link = world["tmp"] / "link"
    link.symlink_to(world["ops"])
    r = run(world, RUNTIME_RELEASE_WT=link / "release")
    assert r.returncode == 5, r.stdout + r.stderr
    assert not (world["ops"] / "release").exists()


def test_new_worktree_inside_another_registered_worktree_is_refused(world):
    other = _linked_worktree(world, "other_wt")
    before = git(other, "status", "--porcelain", "-uall")
    r = run(world, RUNTIME_RELEASE_WT=other / "release")
    assert r.returncode == 5, r.stdout + r.stderr
    assert git(other, "status", "--porcelain", "-uall") == before


def test_new_worktree_in_empty_dir_inside_ops_folder_is_refused(world):
    empty = world["ops"] / "emptydir"
    empty.mkdir()
    r = run(world, RUNTIME_RELEASE_WT=empty)
    assert r.returncode == 5, r.stdout + r.stderr
    assert not (empty / ".git").exists()


def _gitfile_at(wt, gitdir):
    Path(wt).mkdir(parents=True, exist_ok=True)
    (Path(wt) / ".git").write_text(f"gitdir: {gitdir}\n")


def _linked_worktree(world, name):
    path = world["tmp"] / name
    git(world["ops"], "worktree", "add", "--detach", "-q", str(path), "HEAD")
    return path


def test_a_gitfile_pointing_at_main_git_dir_is_refused_and_ops_untouched(world):
    _gitfile_at(world["wt"], world["ops"] / ".git")
    before = ops_snapshot(world["ops"])
    status_before = git(world["ops"], "status", "--porcelain")
    r = run(world)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "UNSAFE_WT" in log_text(world)
    assert ops_snapshot(world["ops"]) == before
    assert git(world["ops"], "status", "--porcelain") == status_before
    assert not world["state"].exists()


def test_a_gitfile_pointing_at_main_git_dir_with_explicit_sha_is_refused(world):
    _gitfile_at(world["wt"], world["ops"] / ".git")
    before = ops_snapshot(world["ops"])
    r = run(world, world["c1"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert ops_snapshot(world["ops"]) == before
    assert git(world["ops"], "rev-parse", "HEAD") == world["c2"]


def test_b_repo_is_linked_worktree_and_target_points_at_its_git_dir_is_refused(world):
    repo_wt = _linked_worktree(world, "ops_linked")
    repo_gd = git(repo_wt, "rev-parse", "--absolute-git-dir")
    assert "/worktrees/" in repo_gd
    _gitfile_at(world["wt"], repo_gd)
    before_repo = ops_snapshot(repo_wt)
    before_main = ops_snapshot(world["ops"])
    status_before = git(repo_wt, "status", "--porcelain")
    r = run(world, RUNTIME_RELEASE_REPO=repo_wt)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "UNSAFE_WT" in log_text(world)
    assert ops_snapshot(repo_wt) == before_repo
    assert ops_snapshot(world["ops"]) == before_main
    assert git(repo_wt, "status", "--porcelain") == status_before


def test_b_repo_is_linked_worktree_and_target_points_at_common_git_dir_is_refused(world):
    repo_wt = _linked_worktree(world, "ops_linked")
    _gitfile_at(world["wt"], world["ops"] / ".git")
    before_repo = ops_snapshot(repo_wt)
    before_main = ops_snapshot(world["ops"])
    r = run(world, RUNTIME_RELEASE_REPO=repo_wt)
    assert r.returncode == 2, r.stdout + r.stderr
    assert ops_snapshot(repo_wt) == before_repo
    assert ops_snapshot(world["ops"]) == before_main


def test_target_pointing_at_unrelated_registered_worktree_git_dir_is_refused(world):
    other = _linked_worktree(world, "someone_elses")
    _gitfile_at(world["wt"], git(other, "rev-parse", "--absolute-git-dir"))
    before = ops_snapshot(other)
    r = run(world)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "not_in_worktree_list" in log_text(world)
    assert ops_snapshot(other) == before


def test_target_with_gitdir_under_worktrees_but_unregistered_is_refused(world):
    fake = world["ops"] / ".git" / "worktrees" / "ghost"
    fake.mkdir(parents=True)
    _gitfile_at(world["wt"], fake)
    before = ops_snapshot(world["ops"])
    r = run(world)
    assert r.returncode == 2, r.stdout + r.stderr
    assert ops_snapshot(world["ops"]) == before


def test_c_proper_linked_worktree_is_refreshed(world):
    wt = world["wt"]
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(world["ops"], "worktree", "add", "--detach", "-q", str(wt), world["c1"])
    before = ops_snapshot(world["ops"])
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(wt, "rev-parse", "HEAD") == world["c3"]
    assert f"REFRESHED prev={world['c1']} new={world['c3']}" in log_text(world)
    assert ops_snapshot(world["ops"]) == before


def test_c_repo_is_linked_worktree_and_target_is_proper_linked_worktree_is_refreshed(world):
    repo_wt = _linked_worktree(world, "ops_linked")
    wt = world["wt"]
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(world["ops"], "worktree", "add", "--detach", "-q", str(wt), world["c1"])
    before_repo = ops_snapshot(repo_wt)
    r = run(world, RUNTIME_RELEASE_REPO=repo_wt)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(wt, "rev-parse", "HEAD") == world["c3"]
    assert ops_snapshot(repo_wt) == before_repo


def test_repo_not_a_git_repository_is_refused(world):
    plain = world["tmp"] / "plain"
    plain.mkdir()
    r = run(world, RUNTIME_RELEASE_REPO=plain)
    assert r.returncode != 0
    assert not world["wt"].exists()


def test_non_empty_non_git_directory_is_refused(world):
    world["wt"].mkdir(parents=True)
    (world["wt"] / "keep.txt").write_text("keep")
    r = run(world)
    assert r.returncode == 5
    assert (world["wt"] / "keep.txt").read_text() == "keep"


def test_fetch_failure_without_explicit_sha_exits_4_and_changes_nothing(world):
    assert run(world, world["c1"]).returncode == 0
    r = run(world, RUNTIME_RELEASE_REMOTE="no-such-remote")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "FETCH_FAILED" in log_text(world)
    assert git(world["wt"], "rev-parse", "HEAD") == world["c1"]


def test_refresh_does_not_run_repository_hooks(world):
    hooks = world["ops"] / ".git/hooks"
    marker = world["tmp"] / "hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    assert run(world, world["c1"]).returncode == 0
    assert run(world).returncode == 0
    assert not marker.exists()


def test_systemd_units_are_stored_in_repo_and_match_contract():
    service = SERVICE.read_text()
    timer = TIMER.read_text()
    assert "Type=oneshot" in service
    assert "TimeoutStartSec=300" in service
    assert "runtime_release_refresh.sh" in service
    # 설치·enable 은 하지 않는다 — 유닛이 스스로 운영 폴더 스크립트를 실행해서도 안 된다
    exec_lines = [ln for ln in service.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    assert "/root/aads/aads-server/" not in exec_lines[0]
    assert "OnCalendar=*:0/5" in timer
    assert "Persistent=true" in timer
    assert "Unit=aads-runtime-release-refresh.service" in timer


# ── scripts/check_runtime_exec_paths.py — 명령 파싱 ─────────────────────────

def _load_checker():
    spec = importlib.util.spec_from_file_location("check_runtime_exec_paths", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


checker = _load_checker()
OPS = "/root/aads/aads-server"
REL = "/root/aads/runtime/aads-server-release"
CFG = checker.Config(ops_root=OPS, release_root=REL)


def classify_cmd(cmd, workdir="", source="cron"):
    return checker.classify(checker.Item(source, "t", cmd, workdir), CFG)


OPS_CASES = [
    f"ionice -c 3 python3 {OPS}/scripts/x.py",
    f"ionice -c3 -n7 python3 {OPS}/scripts/x.py",
    f"sudo -u root python3 {OPS}/scripts/x.py",
    f"sudo -E -H -u root bash {OPS}/scripts/x.sh",
    f"python3 -W ignore {OPS}/scripts/x.py",
    f"python3 -X dev -u -O {OPS}/scripts/x.py",
    f"bash -lc 'cd {OPS} && python3 scripts/x.py'",
    f"sh -ec 'cd {OPS} && ./scripts/x.sh'",
    f"/bin/bash -c \"cd {OPS}; python3 scripts/x.py\"",
    f"nice -n 10 ionice -c3 flock -n /run/lock/x.lock timeout 30s env FOO=1 python3 {OPS}/scripts/x.py",
    f"timeout -k 5 60 chrt -b 0 stdbuf -oL {OPS}/scripts/x.sh",
    f"flock -n /run/lock/x.lock -c 'cd {OPS} && python3 scripts/x.py'",
    f"nohup python3 {OPS}/scripts/x.py >> /var/log/x.log 2>&1",
    f"cd {OPS} && python3 -m app.jobs.nightly",
    f"env -C {OPS} python3 scripts/x.py",
    f"FOO=1 {OPS}/scripts/x.sh",
    f"(cd {OPS} && python3 scripts/x.py)",
    f"{OPS}/.venv/bin/python3 /tmp/x.py",
    f"{OPS}/bin/bash -c 'true'",
    f"{OPS}/bin/timeout 30 python3 /tmp/x.py",
    f"timeout 30 {OPS}/bin/flock -n /l python3 /tmp/x.py",
    f"cd {OPS} && ./bin/python3 /tmp/x.py",
    f"chrt -T 1000000 -o 0 python3 {OPS}/scripts/x.py",
    f"chrt --sched-runtime 1000000 --sched-period 2000000 -d 0 python3 {OPS}/scripts/x.py",
    f"chrt -P 2000000 -D 3000000 -T 1000000 -d 0 {OPS}/scripts/x.sh",
    f"chrt -b 0 python3 {OPS}/scripts/x.py",
]

NOT_OPS_CASES = [
    f"flock -n /run/lock/x.lock cp {OPS}/scripts/x.py /tmp/x.py",
    f"flock -n /run/lock/x.lock rsync -a {OPS}/scripts/x.py /tmp/",
    f"timeout 60 grep -r foo {OPS}/app/main.py",
    f"ionice -c 3 cat {OPS}/scripts/x.sh",
    f"python3 /tmp/helper.py {OPS}/scripts/x.py",
    f"sh -c 'cp {OPS}/a.py /tmp/a.py'",
    f"bash -c 'echo hi' {OPS}/scripts/x.sh",
    f"python3 -c 'print(1)' {OPS}/scripts/x.py",
    f"cd $HOME && python3 scripts/x.py",
    f"docker compose -f {OPS}/docker-compose.yml ps",
]


@pytest.mark.parametrize("cmd", OPS_CASES)
def test_checker_classifies_ops_folder_execution(cmd):
    item = classify_cmd(cmd)
    assert item.cls == "ops_folder", (cmd, [r.as_dict() for r in item.refs])


@pytest.mark.parametrize("cmd", NOT_OPS_CASES)
def test_checker_does_not_treat_ops_data_arguments_as_execution(cmd):
    item = classify_cmd(cmd)
    assert item.cls != "ops_folder", (cmd, [r.as_dict() for r in item.refs])


def test_checker_ops_interpreter_binary_is_reported_as_exec():
    item = classify_cmd(f"{OPS}/.venv/bin/python3 /tmp/x.py")
    assert [(r.role, r.path) for r in item.refs if r.role == "exec"] == [
        ("exec", f"{OPS}/.venv/bin/python3"), ("exec", "/tmp/x.py")]


def test_checker_chrt_value_options_do_not_hide_the_command():
    item = classify_cmd(f"chrt -T 1000000 -o 0 python3 {OPS}/scripts/x.py")
    assert [r.path for r in item.refs if r.role == "exec"] == [f"{OPS}/scripts/x.py"]
    assert classify_cmd("chrt -p 1234").cls == "other"


def test_checker_flock_cp_executable_is_cp_not_the_ops_file():
    item = classify_cmd(f"flock -n /run/lock/x.lock cp {OPS}/scripts/x.py /tmp/x.py")
    assert not [r for r in item.refs if r.role == "exec"]
    assert [r.path for r in item.refs if r.role == "arg"] == [f"{OPS}/scripts/x.py"]


@pytest.mark.parametrize("cmd", [
    f"flock -n /l ionice -c 3 python3 {REL}/scripts/x.py",
    f"cd {REL} && python3 -m app.jobs.nightly",
    f"bash -lc 'cd {REL} && python3 scripts/x.py'",
    f"sudo -u root python3 -W ignore {REL}/scripts/x.py",
])
def test_checker_classifies_release_copy_execution(cmd):
    assert classify_cmd(cmd).cls == "release"


def test_checker_python_dash_m_is_classified_by_cwd():
    assert classify_cmd("python3 -m app.jobs.nightly", workdir=OPS, source="systemd").cls == "ops_folder"
    assert classify_cmd("python3 -m app.jobs.nightly", workdir=REL, source="systemd").cls == "release"
    assert classify_cmd("python3 -m app.jobs.nightly").cls == "other"


def test_checker_systemd_without_workingdirectory_uses_exec_path():
    assert classify_cmd(f"/usr/bin/python3 {OPS}/scripts/x.py", source="systemd").cls == "ops_folder"
    assert classify_cmd(f"/usr/bin/bash {REL}/scripts/x.sh", source="systemd").cls == "release"


def test_checker_relative_exec_resolves_against_cron_cd():
    item = classify_cmd(f"cd {OPS} && ./scripts/x.sh")
    assert any(r.role == "exec" and r.path == f"{OPS}/scripts/x.sh" for r in item.refs)


def test_checker_wrapper_dash_c_is_not_mistaken_for_shell_dash_c():
    # ionice -c 3 의 "3" 을 셸 명령 문자열로 읽지 않고, 뒤따르는 python3 을 실행 파일로 찾는다
    item = classify_cmd(f"ionice -c 3 python3 {OPS}/scripts/x.py")
    assert [r.path for r in item.refs if r.role == "exec"] == [f"{OPS}/scripts/x.py"]


def test_checker_exit_code_reflects_ops_folder_count(tmp_path):
    cron_bad = tmp_path / "bad.cron"
    cron_bad.write_text(
        "# comment\n"
        f"*/5 * * * * flock -n /run/lock/a.lock cp {OPS}/scripts/x.py /tmp/x.py\n"
        f"0 3 * * * ionice -c 3 python3 {OPS}/scripts/y.py\n"
        f"0 4 * * * python3 {REL}/scripts/z.py\n"
    )
    cron_ok = tmp_path / "ok.cron"
    cron_ok.write_text(f"0 4 * * * python3 {REL}/scripts/z.py\n")
    empty_dir = tmp_path / "units"
    empty_dir.mkdir()

    def run_checker(cron):
        return subprocess.run(
            ["python3", str(CHECKER), "--json", "--crontab-file", str(cron), "--systemd-dir", str(empty_dir),
             "--ops-root", OPS, "--release-root", REL],
            capture_output=True, text=True, timeout=60,
        )

    bad = run_checker(cron_bad)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    summary = json.loads(bad.stdout)["summary"]
    assert summary["ops_folder"] == 1 and summary["release"] == 1 and summary["other"] == 1
    ok = run_checker(cron_ok)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_checker_reads_systemd_unit_with_and_without_workingdirectory(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "a.service").write_text(
        f"[Service]\nWorkingDirectory={OPS}\nExecStart=/usr/bin/python3 -m app.jobs.nightly\n")
    (units / "b.service").write_text(f"[Service]\nExecStart=/usr/bin/python3 -W ignore {OPS}/scripts/x.py\n")
    (units / "c.service").write_text(f"[Service]\nExecStart=/usr/bin/bash {REL}/scripts/x.sh\n")
    items = checker.parse_systemd_dir(str(units))
    got = {i.name: checker.classify(i, CFG).cls for i in items}
    assert got == {"a.service": "ops_folder", "b.service": "ops_folder", "c.service": "release"}
