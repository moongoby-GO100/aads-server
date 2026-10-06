"""AAG frontend.roots 해석 — /tmp 러너 워크트리에서도 대시보드를 찾는다.

2026-10-06 runner-cd9acf88 이 코드·테스트 통과 후 커밋 단계에서 막혔다:
`../aads-dashboard/src` 가 /tmp/aads-dashboard/src 로 풀려 0파일 → rc=2.
여기서는 실제 /root/aads 를 건드리지 않고 tmp 에 가짜 저장소/워크트리를 만든다.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "aag_scan_aads_froot", ROOT / "tools" / "aag" / "scan_aads.py"
)
sc = importlib.util.module_from_spec(_spec)
sys.modules["aag_scan_aads_froot"] = sc
_spec.loader.exec_module(sc)

ROOTS = ["../aads-dashboard/src"]
EXCLUDE = ["node_modules", ".git", ".next"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_dashboard(base: Path) -> Path:
    src = base / "aads-dashboard" / "src"
    src.mkdir(parents=True)
    (src / "api.ts").write_text("export const x = 1\n", encoding="utf-8")
    return src


def _make_fake_worktree(tmp: Path) -> tuple[Path, Path]:
    """tmp/main/aads-server(주 저장소) + tmp/wt/runner(.git 파일이 주 저장소를 가리키는 워크트리)."""
    main = tmp / "main" / "aads-server"
    main.mkdir(parents=True)
    _git(main, "init", "-q")
    wt = tmp / "wt" / "runner"
    wt.mkdir(parents=True)
    wt_gitdir = main / ".git" / "worktrees" / "runner"
    wt_gitdir.mkdir(parents=True)
    (wt_gitdir / "HEAD").write_text((main / ".git" / "HEAD").read_text(), encoding="utf-8")
    (wt_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (wt_gitdir / "gitdir").write_text(f"{wt}/.git\n", encoding="utf-8")
    (wt / ".git").write_text(f"gitdir: {wt_gitdir}\n", encoding="utf-8")
    return main, wt


def _files(root: Path, roots=ROOTS) -> list[str]:
    return sc.iter_files(root, sc.resolve_frontend_roots(root, roots), [".ts", ".tsx"], EXCLUDE)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("AADS_DASHBOARD_WORKDIR", raising=False)


def test_root_relative_path_wins_when_it_exists(tmp_path, monkeypatch, capsys):
    server = tmp_path / "aads-server"
    server.mkdir()
    src = _make_dashboard(tmp_path)
    other = tmp_path / "other"
    _make_dashboard(other)
    monkeypatch.setenv("AADS_DASHBOARD_WORKDIR", str(other / "aads-dashboard"))

    assert sc.resolve_frontend_roots(server, ROOTS) == ROOTS
    assert _files(server) == [(src / "api.ts").resolve().as_posix()]
    assert "대체 해석" not in capsys.readouterr().err


def test_env_workdir_used_when_root_relative_missing(tmp_path, monkeypatch, capsys):
    server = tmp_path / "wt" / "aads-server"
    server.mkdir(parents=True)
    src = _make_dashboard(tmp_path / "elsewhere")
    monkeypatch.setenv("AADS_DASHBOARD_WORKDIR", str(tmp_path / "elsewhere" / "aads-dashboard"))

    assert _files(server) == [(src / "api.ts").resolve().as_posix()]
    err = capsys.readouterr().err
    assert "[AAG] frontend root 대체 해석: " in err
    assert str(src.resolve()) in err and " → " in err


def test_env_workdir_substitutes_only_matching_sub_path(tmp_path, monkeypatch):
    server = tmp_path / "aads-server"
    server.mkdir()
    _make_dashboard(tmp_path / "elsewhere")
    monkeypatch.setenv("AADS_DASHBOARD_WORKDIR", str(tmp_path / "elsewhere" / "aads-dashboard"))

    assert _files(server, ["../other-repo/src"]) == []


def test_git_common_dir_resolves_in_worktree_without_env(tmp_path, capsys):
    main, wt = _make_fake_worktree(tmp_path)
    src = _make_dashboard(tmp_path / "main")
    assert not (wt.parent / "aads-dashboard").exists()

    assert _files(wt) == [(src / "api.ts").resolve().as_posix()]
    err = capsys.readouterr().err
    assert "[AAG] frontend root 대체 해석: " in err
    assert str((wt.parent / "aads-dashboard" / "src")) in err


def test_keys_identical_between_main_checkout_and_worktree(tmp_path):
    main, wt = _make_fake_worktree(tmp_path)
    _make_dashboard(tmp_path / "main")

    assert _files(wt) == _files(main)


def test_env_workdir_takes_precedence_over_git_common_dir(tmp_path, monkeypatch):
    main, wt = _make_fake_worktree(tmp_path)
    _make_dashboard(tmp_path / "main")
    envsrc = _make_dashboard(tmp_path / "env")
    monkeypatch.setenv("AADS_DASHBOARD_WORKDIR", str(tmp_path / "env" / "aads-dashboard"))

    assert _files(wt) == [(envsrc / "api.ts").resolve().as_posix()]


def test_all_candidates_missing_stays_fail_closed(tmp_path, monkeypatch):
    main, wt = _make_fake_worktree(tmp_path)
    monkeypatch.setenv("AADS_DASHBOARD_WORKDIR", str(tmp_path / "nope"))

    assert sc.resolve_frontend_roots(wt, ROOTS) == ROOTS
    scan = SimpleNamespace(
        py_files=["a.py"], router_dir_files=["a.py"], modules={"m": 1},
        entrypoints_present=True, fe_files=_files(wt), rules={"frontend": {"roots": ROOTS}},
    )
    reasons = sc.zero_target_guard(scan)
    assert any("frontend.roots" in r for r in reasons)


def test_non_git_root_without_env_stays_fail_closed(tmp_path):
    plain = tmp_path / "plain" / "aads-server"
    plain.mkdir(parents=True)

    assert _files(plain) == []
