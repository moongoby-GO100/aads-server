import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import unified_component_deploy_worker as worker  # noqa: E402
from app.services.deploy_adapters.targets import ExecutionTarget  # noqa: E402

SHA = "651675d7fe09bde7b8bd92f7f9cb27845b4454a1"


def _target(project="ACCT", component="app", executor="local"):
    return ExecutionTarget(project, component, "app", executor, "", 0, "/repo", ("true",))


def _install(monkeypatch, tmp_path, cat_results, fetch_rc=0, fetch_err=""):
    monkeypatch.setenv("AADS_COMPONENT_RELEASE_ROOT", str(tmp_path))
    calls = []
    cats = list(cat_results)

    def fake(target, command, timeout=60):
        calls.append(command)
        rc, out, err = 0, "", ""
        if "cat-file" in command:
            rc = cats.pop(0)
        elif "fetch" in command:
            rc, err = fetch_rc, fetch_err
        elif "worktree" in command:
            Path(command[-2]).mkdir(parents=True)
            (Path(command[-2]) / "scripts").mkdir()
            (Path(command[-2]) / "scripts/deploy_acct_app_cafe24.sh").write_text("")
        elif "rev-parse" in command:
            out = SHA
        return subprocess.CompletedProcess(command, rc, out, err)

    monkeypatch.setattr(worker, "run_command", fake)
    return calls


def _count(calls, word):
    return sum(1 for c in calls if word in c)


def test_object_present_no_fetch(monkeypatch, tmp_path):
    calls = _install(monkeypatch, tmp_path, [0])
    worker.prepare_release_target(_target(), SHA, 1)
    assert _count(calls, "fetch") == 0
    assert _count(calls, "worktree") == 1


def test_missing_object_fetched_then_added(monkeypatch, tmp_path):
    calls = _install(monkeypatch, tmp_path, [1, 0])
    worker.prepare_release_target(_target(), SHA, 2)
    assert _count(calls, "fetch") == 1
    assert _count(calls, "worktree") == 1
    assert not any(c for c in calls if {"checkout", "reset", "pull", "merge", "rebase"} & set(c))


def test_still_missing_after_fetch(monkeypatch, tmp_path):
    calls = _install(monkeypatch, tmp_path, [1, 1])
    with pytest.raises(RuntimeError, match="fetch 후에도 SHA 없음"):
        worker.prepare_release_target(_target(), SHA, 3)
    assert _count(calls, "worktree") == 0


def test_fetch_failure_includes_stderr(monkeypatch, tmp_path):
    calls = _install(monkeypatch, tmp_path, [1], fetch_rc=128, fetch_err="network down")
    with pytest.raises(RuntimeError, match="network down"):
        worker.prepare_release_target(_target(), SHA, 4)
    assert _count(calls, "worktree") == 0


def test_non_acct_target_untouched(monkeypatch, tmp_path):
    calls = _install(monkeypatch, tmp_path, [])
    t = _target(project="KIS")
    assert worker.prepare_release_target(t, SHA, 5) is t
    assert calls == []
