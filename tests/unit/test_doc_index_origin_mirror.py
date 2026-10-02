"""문서 색인 원본 미러 전환 회귀 테스트 (AADS-DOC-INDEX-ORIGIN-MIRROR-20261002).

index_docs.py 가 뒤처진 개발 작업트리 대신 origin/main 미러를 읽되, doc_chunks 에
저장되는 경로(doc_path)는 기존 /root/aads/aads-server/... 그대로여야 한다.
경로가 바뀌면 전 문서가 중복 색인되고 옛 행이 지워진다.
"""
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "index_docs.py"
_REFRESH = _REPO / "scripts" / "refresh_docs_mirror.sh"
LIVE = "/root/aads/aads-server"


def _load():
    spec = importlib.util.spec_from_file_location("index_docs_mirror", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_mirror(tmp_path: Path) -> Path:
    m = tmp_path / "mirror"
    (m / ".git").mkdir(parents=True)
    (m / "docs" / "sub").mkdir(parents=True)
    (m / "reports").mkdir()
    return m


def test_resolve_root_maps_docs_and_reports_to_mirror():
    m = _load()
    mirror = "/root/aads/mirrors/aads-server"
    assert m.resolve_root(f"{LIVE}/docs", mirror, use_mirror=True) == f"{mirror}/docs"
    assert m.resolve_root(f"{LIVE}/reports", mirror, use_mirror=True) == f"{mirror}/reports"
    assert m.resolve_root(f"{LIVE}/docs/sub", mirror, use_mirror=True) == f"{mirror}/docs/sub"


def test_resolve_root_leaves_other_roots_alone():
    m = _load()
    mirror = "/root/aads/mirrors/aads-server"
    for root in (
        "/root/aads/aads-docs/docs", "/app/docs", f"{LIVE}/AGENTS.md",
        f"{LIVE}/app/static/reports", f"{LIVE}/docs-old", "/root/aads/_remote_docs/x",
    ):
        assert m.resolve_root(root, mirror, use_mirror=True) == root


def test_resolve_root_falls_back_without_mirror(tmp_path):
    m = _load()
    missing = str(tmp_path / "nope")
    assert not m.mirror_ready(missing)
    assert m.resolve_root(f"{LIVE}/docs", missing) == f"{LIVE}/docs"
    assert m.resolve_root(f"{LIVE}/reports", missing, use_mirror=False) == f"{LIVE}/reports"


def test_mirror_ready_requires_git_and_docs(tmp_path):
    m = _load()
    mirror = _make_mirror(tmp_path)
    assert m.mirror_ready(str(mirror))
    shutil.rmtree(mirror / ".git")
    assert not m.mirror_ready(str(mirror))


def test_mirror_dir_env_override(monkeypatch):
    m = _load()
    monkeypatch.delenv("AADS_DOCS_MIRROR", raising=False)
    assert m.mirror_dir() == "/root/aads/mirrors/aads-server"
    monkeypatch.setenv("AADS_DOCS_MIRROR", "/x/y")
    assert m.mirror_dir() == "/x/y"


def test_identity_path_is_unchanged_by_mirror():
    m = _load()
    mirror = "/root/aads/mirrors/aads-server"
    for rel in ("docs/a.md", "docs/routing-ssot/DESIGN.md", "reports/20261002_x.md"):
        logical_root = f"{LIVE}/{rel.split('/')[0]}"
        phys_root = m.resolve_root(logical_root, mirror, use_mirror=True)
        physical = f"{mirror}/{rel}"
        assert m.to_logical_path(physical, logical_root, phys_root) == f"{LIVE}/{rel}"
    assert m.to_logical_path("/a/b.md", "/a/b.md", "/a/b.md") == "/a/b.md"


def test_roots_keep_logical_paths():
    m = _load()
    roots = [r[0] for r in m.ROOTS]
    assert f"{LIVE}/docs" in roots and f"{LIVE}/reports" in roots
    assert not any("/mirrors/" in r for r in roots)


def test_collect_reads_mirror_but_stores_logical_path(tmp_path, monkeypatch):
    m = _load()
    mirror = _make_mirror(tmp_path)
    body = "# 신규 문서\n\n" + ("origin 에만 있는 본문. " * 40)
    (mirror / "docs" / "sub" / "new.md").write_text(body, encoding="utf-8")
    (mirror / "reports" / "r.md").write_text(body + "리포트", encoding="utf-8")
    monkeypatch.setenv("AADS_DOCS_MIRROR", str(mirror))
    monkeypatch.setattr(m, "ROOTS", [
        (f"{LIVE}/docs", "AADS", "서버 문서"),
        (f"{LIVE}/reports", "AADS", "서버 리포트"),
    ])
    docs = m.collect()
    assert [d["path"] for d in docs] == [f"{LIVE}/docs/sub/new.md", f"{LIVE}/reports/r.md"]
    assert all(str(mirror) not in d["path"] for d in docs)
    assert {d["label"] for d in docs} == {"서버 문서", "서버 리포트"}


def test_collect_mirror_and_fallback_yield_same_keys(tmp_path, monkeypatch):
    """미러 유무와 무관하게 같은 문서는 같은 doc_path 키를 낸다."""
    m = _load()
    mirror = _make_mirror(tmp_path)
    body = "# 같은 문서\n\n" + ("본문 " * 100)
    (mirror / "docs" / "same.md").write_text(body, encoding="utf-8")
    monkeypatch.setenv("AADS_DOCS_MIRROR", str(mirror))
    monkeypatch.setattr(m, "ROOTS", [(f"{LIVE}/docs", "AADS", "서버 문서")])
    via_mirror = m.collect()

    # 폴백 경로: LIVE_TREE 를 임시 작업트리로 돌려 같은 상대 경로로 읽게 한다.
    live = tmp_path / "live"
    (live / "docs").mkdir(parents=True)
    (live / "docs" / "same.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(m, "LIVE_TREE", str(live))
    monkeypatch.setenv("AADS_DOCS_MIRROR", str(tmp_path / "absent"))
    monkeypatch.setattr(m, "ROOTS", [(f"{live}/docs", "AADS", "서버 문서")])
    via_fallback = m.collect()

    assert [d["path"].removeprefix(LIVE) for d in via_mirror] == \
           [d["path"].removeprefix(str(live)) for d in via_fallback] == ["/docs/same.md"]
    assert via_mirror[0]["sha256"] == via_fallback[0]["sha256"]


def test_collect_warns_on_fallback(tmp_path, monkeypatch, capsys):
    m = _load()
    live = tmp_path / "live"
    (live / "docs").mkdir(parents=True)
    (live / "docs" / "a.md").write_text("# a\n\n" + "본문 " * 100, encoding="utf-8")
    monkeypatch.setattr(m, "LIVE_TREE", str(live))
    monkeypatch.setenv("AADS_DOCS_MIRROR", str(tmp_path / "absent"))
    monkeypatch.setattr(m, "ROOTS", [(f"{live}/docs", "AADS", "서버 문서")])
    docs = m.collect()
    assert len(docs) == 1
    assert "폴백" in capsys.readouterr().err


# ── refresh_docs_mirror.sh ─────────────────────────────────────────────

needs_tools = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("flock") and shutil.which("bash")),
    reason="git/flock 필요",
)


def _run_refresh(env_extra: dict, tmp_path: Path):
    env = dict(os.environ)
    env.update({
        "DOCS_MIRROR_LOCK": str(tmp_path / "lock"),
        "DOCS_MIRROR_LIVE_TREE": str(tmp_path / "live"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
    })
    env.update(env_extra)
    return subprocess.run(["bash", str(_REFRESH)], env=env, capture_output=True,
                          text=True, timeout=60)


def _git(cwd: Path, *args: str):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main",
         *args], cwd=cwd, check=True, capture_output=True,
    )


def test_refresh_script_has_guards():
    src = _REFRESH.read_text(encoding="utf-8")
    assert "flock -n" in src and "timeout" in src
    assert "reset --hard" in src and "clean -fdq" in src
    assert "overlaps" in src
    # 개발 작업트리에는 git 명령을 보내지 않는다 — git 호출은 mgit/-C "$MIRROR" 뿐이다.
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#") or "git" not in s:
            continue
        if "LIVE_TREE" in s and any(c in s for c in ("git -C", "git fetch", "git reset", "git clean")):
            pytest.fail(f"개발 작업트리에 git 실행 가능성: {line}")


@needs_tools
def test_refresh_clones_updates_and_records_sha(tmp_path):
    origin = tmp_path / "o.git"
    work = tmp_path / "w"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "docs").mkdir()
    (work / "docs" / "a.md").write_text("a", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "1")
    _git(work, "push", "-q", str(origin), "main")

    mirror = tmp_path / "m" / "aads-server"
    env = {"DOCS_MIRROR_DIR": str(mirror), "DOCS_MIRROR_ORIGIN_URL": str(origin)}
    r = _run_refresh(env, tmp_path)
    assert r.returncode == 0, r.stderr
    assert (mirror / "docs" / "a.md").exists()

    (work / "docs" / "b.md").write_text("b", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "2")
    _git(work, "push", "-q", str(origin), "main")
    (mirror / "junk.txt").write_text("x", encoding="utf-8")
    r = _run_refresh(env, tmp_path)
    assert r.returncode == 0, r.stderr
    assert (mirror / "docs" / "b.md").exists()
    assert not (mirror / "junk.txt").exists()
    sha = (tmp_path / "m" / "aads-server.last_ok").read_text().split()[0]
    assert len(sha) == 40 and sha in r.stdout


@needs_tools
def test_refresh_refuses_live_worktree_and_non_repo(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    r = _run_refresh({"DOCS_MIRROR_DIR": str(live / "sub")}, tmp_path)
    assert r.returncode == 2 and not (live / "sub").exists()
    r = _run_refresh({"DOCS_MIRROR_DIR": str(live)}, tmp_path)
    assert r.returncode == 2

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "keep.txt").write_text("k", encoding="utf-8")
    r = _run_refresh({"DOCS_MIRROR_DIR": str(plain), "DOCS_MIRROR_ORIGIN_URL": "x"}, tmp_path)
    assert r.returncode == 2 and (plain / "keep.txt").exists()


@needs_tools
def test_refresh_fails_nonzero_when_origin_unreachable(tmp_path):
    r = _run_refresh({"DOCS_MIRROR_DIR": str(tmp_path / "m"),
                      "DOCS_MIRROR_ORIGIN_URL": str(tmp_path / "missing.git")}, tmp_path)
    assert r.returncode == 1
    assert "마지막 성공 SHA" in r.stderr
    assert not (tmp_path / "m").exists()


@needs_tools
def test_refresh_lock_prevents_concurrent_run(tmp_path):
    import fcntl
    lock = tmp_path / "lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _run_refresh({"DOCS_MIRROR_DIR": str(tmp_path / "m"),
                          "DOCS_MIRROR_ORIGIN_URL": "x"}, tmp_path)
    assert r.returncode == 75
