"""Verify release isolation with real Git worktrees and no deploy/DB calls."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("component_release_worker", ROOT / "scripts/unified_component_deploy_worker.py")
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@example.test", *args], text=True).strip()


@pytest.fixture
def release_source(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    script = repo / "scripts/deploy_acct_app_cafe24.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/bash\necho committed-release\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "release")
    sha = git(repo, "rev-parse", "HEAD")
    script.write_text("#!/bin/bash\necho unrelated-uncommitted-work\n")
    monkeypatch.setenv("AADS_COMPONENT_RELEASE_ROOT", str(tmp_path / "releases"))
    target = worker.ExecutionTarget(project="ACCT", component="app", deploy_type="container_replace",
        executor="local", host="local", port=22, repo_path=str(repo), command=("bash", str(script)))
    return repo, script, sha, target


def test_dirty_shared_source_is_preserved_and_committed_release_script_is_selected(release_source):
    repo, source_script, sha, target = release_source
    isolated = worker.prepare_release_target(target, sha, 1)
    worker.preflight(isolated, sha)
    assert isolated.require_clean_repo is True
    assert isolated.repo_path != str(repo)
    assert Path(isolated.command[1]).read_text().endswith("echo committed-release\n")
    assert source_script.read_text().endswith("echo unrelated-uncommitted-work\n")
    assert git(repo, "status", "--porcelain")
    assert worker.prepare_release_target(target, sha, 1) == isolated


def test_dirty_existing_release_is_still_blocked(release_source):
    _, _, sha, target = release_source
    isolated = worker.prepare_release_target(target, sha, 2)
    Path(isolated.command[1]).write_text("changed\n")
    with pytest.raises(RuntimeError, match="dirty deployment repository"):
        worker.preflight(worker.prepare_release_target(target, sha, 2), sha)


def test_wrong_head_existing_release_is_never_reset(release_source):
    _, _, sha, target = release_source
    isolated = worker.prepare_release_target(target, sha, 3)
    Path(isolated.command[1]).write_text("changed\n")
    git(isolated.repo_path, "commit", "-qam", "other work")
    with pytest.raises(RuntimeError, match="HEAD does not match"):
        worker.prepare_release_target(target, sha, 3)
    assert Path(isolated.command[1]).read_text() == "changed\n"


def test_other_targets_keep_the_original_clean_repository_gate(release_source):
    _, _, sha, target = release_source
    target = worker.replace(target, project="NTV2")
    assert worker.prepare_release_target(target, sha, 4) is target
    with pytest.raises(RuntimeError, match="dirty deployment repository"):
        worker.preflight(target, sha)
