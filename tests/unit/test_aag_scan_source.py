"""AADS 스캔 소스를 origin/main 전용 clean 클론으로 분리하는 계약 테스트.

실제 git 으로 돈다(네트워크 없음, 로컬 bare origin). 커밋은 porcelain 이 아니라
plumbing(hash-object/write-tree/commit-tree/update-ref)으로 만든다 — 테스트가
어떤 저장소의 인덱스나 워킹트리도 건드리지 않는다는 것을 구조로 보장한다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import aag_prepare_source as prep  # noqa: E402

IDENT = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(*args, cwd=None, env=None, stdin=None):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, text=True, capture_output=True,
        env={**os.environ, **IDENT, **(env or {})}, check=True,
    )
    return proc.stdout.strip()


def commit_to_origin(origin: Path, files: dict, message: str) -> str:
    """Create a commit on origin's main using plumbing only."""
    env = {"GIT_DIR": str(origin), "GIT_INDEX_FILE": str(origin / "tmp-index")}
    (origin / "tmp-index").unlink(missing_ok=True)
    for name, content in files.items():
        blob = git("hash-object", "-w", "--stdin", stdin=content, env=env)
        git("update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, name), env=env)
    tree = git("write-tree", env=env)
    parent = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "--verify", "-q", "refs/heads/main"],
        capture_output=True, text=True,
    ).stdout.strip()
    args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    sha = git(*args, env=env)
    git("update-ref", "refs/heads/main", sha, env=env)
    return sha


@pytest.fixture
def origin(tmp_path):
    repo = tmp_path / "origin.git"
    git("init", "--bare", "--quiet", str(repo))
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)
    commit_to_origin(repo, {"app.py": "print(1)\n"}, "c1")
    return repo


def head_of(path: Path) -> str:
    return git("rev-parse", "HEAD", cwd=path)


def test_first_run_clones_and_pins_detached_origin_head(origin, tmp_path):
    dest = tmp_path / "src" / "aads-server"
    info = prep.prepare_source(dest, str(origin))
    assert info["state"] == "cloned"
    assert info["head"] == info["expected"] == git("rev-parse", "refs/heads/main", cwd=origin)
    assert (dest / "app.py").read_text() == "print(1)\n"
    # detached: symbolic-ref fails
    assert subprocess.run(["git", "-C", str(dest), "symbolic-ref", "-q", "HEAD"]).returncode != 0


def test_already_current_is_a_noop(origin, tmp_path):
    dest = tmp_path / "aads-server"
    prep.prepare_source(dest, str(origin))
    info = prep.prepare_source(dest, str(origin))
    assert info["state"] == "current"
    assert info["head"] == info["expected"]


def test_behind_clone_advances_to_new_origin_head(origin, tmp_path):
    dest = tmp_path / "aads-server"
    first = prep.prepare_source(dest, str(origin))["head"]
    c2 = commit_to_origin(origin, {"app.py": "print(2)\n", "new.py": "x = 1\n"}, "c2")
    info = prep.prepare_source(dest, str(origin))
    assert info["state"] == "updated"
    assert first != c2 and info["head"] == info["expected"] == c2 == head_of(dest)
    assert (dest / "app.py").read_text() == "print(2)\n" and (dest / "new.py").exists()


@pytest.mark.parametrize("mutate", ["untracked", "modified"])
def test_dirty_source_is_rejected_and_left_untouched(origin, tmp_path, mutate):
    dest = tmp_path / "aads-server"
    before = prep.prepare_source(dest, str(origin))["head"]
    if mutate == "untracked":
        (dest / "stray.txt").write_text("someone's work\n")
    else:
        (dest / "app.py").write_text("print('edited')\n")
    commit_to_origin(origin, {"app.py": "print(3)\n"}, "c3")
    with pytest.raises(prep.SourceRejected) as exc:
        prep.prepare_source(dest, str(origin))
    assert exc.value.reason == "dirty_source"
    assert head_of(dest) == before
    if mutate == "untracked":
        assert (dest / "stray.txt").read_text() == "someone's work\n"
    else:
        assert (dest / "app.py").read_text() == "print('edited')\n"


def test_origin_mismatch_is_rejected(origin, tmp_path):
    dest = tmp_path / "aads-server"
    prep.prepare_source(dest, str(origin))
    with pytest.raises(prep.SourceRejected) as exc:
        prep.prepare_source(dest, str(tmp_path / "other.git"))
    assert exc.value.reason == "origin_mismatch"


def test_dest_inside_another_repository_is_rejected(origin, tmp_path):
    outer = tmp_path / "aads-server"
    prep.prepare_source(outer, str(origin))
    nested = outer / "sub"
    nested.mkdir()
    (nested / ".git").mkdir()
    with pytest.raises(prep.SourceRejected) as exc:
        prep.prepare_source(nested, str(origin))
    assert exc.value.reason == "not_own_repo"


def test_non_empty_non_repo_dest_is_rejected(origin, tmp_path):
    dest = tmp_path / "aads-server"
    dest.mkdir()
    (dest / "keep.txt").write_text("x")
    with pytest.raises(prep.SourceRejected) as exc:
        prep.prepare_source(dest, str(origin))
    assert exc.value.reason == "dest_not_empty"
    assert (dest / "keep.txt").exists()


def test_unreachable_origin_is_an_error_not_a_rejection(tmp_path):
    with pytest.raises(prep.SourceError):
        prep.prepare_source(tmp_path / "aads-server", str(tmp_path / "missing.git"))
    assert not (tmp_path / "aads-server").exists()
    assert not (tmp_path / "aads-server.cloning").exists()


def test_host_checkout_is_never_modified(origin, tmp_path, capsys):
    """origin URL 조회 외에 호스트 체크아웃(--origin-from)을 건드리지 않는다."""
    host = tmp_path / "host" / "aads-server"
    host.parent.mkdir()
    git("clone", "--quiet", str(origin), str(host))
    (host / "app.py").write_text("print('staged by another session')\n")
    git("update-index", "--add", "app.py", cwd=host)  # staged change, as another session leaves it
    (host / "scratch.sh").write_text("#!/bin/sh\n")
    snapshot = lambda: (  # noqa: E731
        git("status", "--porcelain=v1", "--untracked-files=all", cwd=host),
        head_of(host),
        git("rev-parse", "refs/remotes/origin/main", cwd=host),
        (host / "app.py").read_text(),
        git("diff", "--cached", "--stat", cwd=host),
        git("show-ref", cwd=host),
        git("reflog", cwd=host),
    )
    before = snapshot()
    commit_to_origin(origin, {"app.py": "print(9)\n"}, "c9")
    dest = tmp_path / "src" / "aads-server"
    rc = prep.main(["--dest", str(dest), "--origin-from", str(host)])
    out = capsys.readouterr().out
    assert rc == prep.EXIT_OK and "AAG_SOURCE root=" in out
    assert snapshot() == before
    assert head_of(dest) == git("rev-parse", "refs/heads/main", cwd=origin)


def test_cli_exit_codes_and_output(origin, tmp_path, capsys):
    dest = tmp_path / "aads-server"
    assert prep.main(["--dest", str(dest), "--origin-url", str(origin)]) == prep.EXIT_OK
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert out.startswith("AAG_SOURCE root=") and " state=cloned" in out
    (dest / "stray.txt").write_text("x")
    assert prep.main(["--dest", str(dest), "--origin-url", str(origin)]) == prep.EXIT_REJECTED
    assert "AAG_SOURCE_REJECTED reason=dirty_source" in capsys.readouterr().out
    assert prep.main(["--dest", str(tmp_path / "x"), "--origin-url", str(tmp_path / "no.git")]) == prep.EXIT_ERROR


def test_cli_refuses_to_use_the_origin_source_as_dest(origin, tmp_path, capsys):
    host = tmp_path / "aads-server"
    git("clone", "--quiet", str(origin), str(host))
    rc = prep.main(["--dest", str(host), "--origin-from", str(host)])
    assert rc == prep.EXIT_REJECTED
    assert "dest_is_origin_source" in capsys.readouterr().out


def test_mask_url_hides_credentials():
    assert prep.mask_url("https://x-access-token:abc@github.com/o/r.git") == "https://***@github.com/o/r.git"
    assert prep.mask_url("git@github.com:o/r.git") == "git@github.com:o/r.git"


# ── aag_scan_aads_clean.sh (dry-run wrapper) ────────────────────────────

STUB_SCANNER = """import json, os, sys
out = sys.argv[sys.argv.index("--out-dir") + 1]
root = sys.argv[sys.argv.index("--root") + 1]
os.makedirs(out, exist_ok=True)
if os.environ.get("STUB_SCAN_FAIL"):
    sys.exit(int(os.environ["STUB_SCAN_FAIL"]))
sha = os.environ["STUB_GRAPH_SHA"] if os.environ.get("STUB_GRAPH_SHA") else os.environ["AADS_COMMIT_SHA"]
with open(os.path.join(out, "aads-graph.json"), "w") as fh:
    json.dump({"source_identity": {"resolved_commit_sha": sha,
               "expected_target_ref_head_sha": os.environ["AAG_EXPECTED_TARGET_REF_HEAD_SHA"],
               "target_ref": os.environ["AAG_TARGET_REF"],
               "repository_id": os.environ["AAG_REPOSITORY_ID"], "root": root}}, fh)
sys.exit(1)
"""


@pytest.fixture
def wrapper_env(origin, tmp_path):
    dash = tmp_path / "dash.git"
    git("init", "--bare", "--quiet", str(dash))
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=dash)
    commit_to_origin(dash, {"x.ts": "export {}\n"}, "d1")
    scanner = tmp_path / "scanner"
    scanner.mkdir()
    (scanner / "scan_aads.py").write_text(STUB_SCANNER)
    (scanner / "rules_aads.yml").write_text("{}\n")
    state = tmp_path / "state"
    env = {
        **os.environ,
        "AAG_STATE_DIR": str(state),
        "AAG_AADS_ORIGIN_URL": str(origin),
        "AAG_DASHBOARD_ORIGIN_URL": str(dash),
        "AAG_SCANNER_DIR": str(scanner),
    }
    return env, state


def _wrapper(env, **extra):
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "aag_scan_aads_clean.sh")],
        env={**env, **extra}, capture_output=True, text=True, timeout=120,
    )


def test_wrapper_scans_clean_clone_at_origin_head_and_writes_outside_repos(wrapper_env, origin):
    import json
    env, state = wrapper_env
    proc = _wrapper(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    expected = git("rev-parse", "refs/heads/main", cwd=origin)
    assert "AAG_SCAN_AADS head=%s expected=%s" % (expected, expected) in proc.stdout
    graph = json.loads((state / "aads" / "aads-graph.json").read_text())
    ident = graph["source_identity"]
    assert ident["resolved_commit_sha"] == ident["expected_target_ref_head_sha"] == expected
    assert ident["target_ref"] == "refs/heads/main" and ident["repository_id"] == "aads-server"
    assert ident["root"] == str(state / "src" / "aads-server")
    assert git("status", "--porcelain", "--untracked-files=all", cwd=state / "src" / "aads-server") == ""
    assert (state / "src" / "aads-dashboard" / "x.ts").exists()


def test_wrapper_follows_origin_forward_each_run(wrapper_env, origin):
    env, _ = wrapper_env
    assert _wrapper(env).returncode == 0
    c2 = commit_to_origin(origin, {"app.py": "print(2)\n"}, "c2")
    proc = _wrapper(env)
    assert proc.returncode == 0 and "head=%s expected=%s" % (c2, c2) in proc.stdout


def test_wrapper_dirty_source_exits_3_without_scanning(wrapper_env):
    env, state = wrapper_env
    assert _wrapper(env).returncode == 0
    (state / "aads" / "aads-graph.json").unlink()
    (state / "src" / "aads-server" / "stray.txt").write_text("x")
    proc = _wrapper(env)
    assert proc.returncode == 3
    assert "AAG_SOURCE_REJECTED reason=dirty_source" in proc.stdout
    assert not (state / "aads" / "aads-graph.json").exists()


def test_wrapper_scan_failure_exits_4_and_leaves_no_stale_graph(wrapper_env):
    env, state = wrapper_env
    assert _wrapper(env).returncode == 0
    assert (state / "aads" / "aads-graph.json").exists()
    proc = _wrapper(env, STUB_SCAN_FAIL="2")
    assert proc.returncode == 4
    assert not (state / "aads" / "aads-graph.json").exists()


def test_wrapper_rejects_graph_whose_sha_is_not_the_scanned_head(wrapper_env):
    env, _ = wrapper_env
    proc = _wrapper(env, STUB_GRAPH_SHA="d" * 40)
    assert proc.returncode == 4 and "verify_graph_sha" in proc.stdout


def test_clone_filter_is_forwarded_only_to_the_initial_clone(origin, tmp_path):
    calls = []

    def spy(args, cwd, timeout):
        calls.append(list(args))
        return prep.run_git(args, cwd, timeout)

    dest = tmp_path / "aads-server"
    prep.prepare_source(dest, str(origin), git=spy, clone_filter="blob:none")
    prep.prepare_source(dest, str(origin), git=spy, clone_filter="blob:none")
    clones = [c for c in calls if c[0] == "clone"]
    assert len(clones) == 1 and "--filter=blob:none" in clones[0]
