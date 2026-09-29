"""ACCT appears in the central ledger without joining health monitoring."""

import asyncio

from app.services.server_registry import (
    ALL_PROJECTS,
    CANONICAL_SERVER_IDS,
    LEDGER_SERVER_IDS,
    PROJECT_TO_SERVER,
    get_server_host,
    list_ledger_servers,
    list_servers,
)
from app.services.deploy_observability import git_release_preflight
from app.services.deploy_adapters.base import BaseDeployAdapter, DeployRequest


def test_acct_project_maps_to_jinah244():
    assert PROJECT_TO_SERVER["ACCT"] == "jinah244"
    assert "ACCT" in ALL_PROJECTS
    assert get_server_host("jinah244") == "5.104.85.244"


def test_jinah244_is_not_health_monitored():
    assert "jinah244" not in CANONICAL_SERVER_IDS
    assert "jinah244" not in [server["id"] for server in list_servers()]


def test_jinah244_is_in_ledger():
    assert "jinah244" in LEDGER_SERVER_IDS
    assert "jinah244" in [server["id"] for server in list_ledger_servers()]


def _acct_adapter():
    from app.services.deploy_adapters.base import OWNER_PROJECT_RUNNER

    class AcctAdapter(BaseDeployAdapter):
        project = "ACCT"
        ownership = OWNER_PROJECT_RUNNER

    return AcctAdapter()


def _request(project, sha):
    return DeployRequest(project=project, component="api", deploy_type="api_bluegreen", release_sha=sha)


def _make_repo(path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_acct_empty_repo_path_skips_preflight_with_warning(monkeypatch):
    monkeypatch.setenv("ACCT_REPO_PATH", "")
    details = git_release_preflight("ACCT", "abcdef0")
    assert details["preflight_skipped"] == "acct_preflight_skipped_remote_repo"
    assert not details.get("config_error")

    result = asyncio.run(_acct_adapter().preflight(_request("ACCT", "abcdef0")))
    assert result.ok
    assert result.blockers == []
    assert "acct_preflight_skipped_remote_repo" in result.warnings


def test_acct_repo_path_set_runs_normal_preflight(monkeypatch, tmp_path):
    sha = _make_repo(tmp_path)
    monkeypatch.setenv("ACCT_REPO_PATH", str(tmp_path))
    details = git_release_preflight("ACCT", sha)
    assert "preflight_skipped" not in details
    assert details["repo_path"] == str(tmp_path)
    assert details["release_known"] is True

    ok = asyncio.run(_acct_adapter().preflight(_request("ACCT", sha)))
    assert ok.ok and "acct_preflight_skipped_remote_repo" not in ok.warnings

    bad = asyncio.run(_acct_adapter().preflight(_request("ACCT", "deadbeef")))
    assert not bad.ok
    assert "release_sha_not_found_in_repo:deadbeef" in bad.blockers


def test_aads_preflight_blocker_unchanged(monkeypatch, tmp_path):
    from app.services import deploy_observability as obs

    sha = _make_repo(tmp_path)
    monkeypatch.setitem(obs.PROJECT_REPO_PATHS, "AADS", (str(tmp_path),))
    details = git_release_preflight("AADS", "deadbeef")
    assert "preflight_skipped" not in details
    assert details["release_known"] is False

    class AadsAdapter(BaseDeployAdapter):
        project = "AADS"
        ownership = "project_runner"

    bad = asyncio.run(AadsAdapter().preflight(_request("AADS", "deadbeef")))
    assert not bad.ok
    assert "release_sha_not_found_in_repo:deadbeef" in bad.blockers
    good = asyncio.run(AadsAdapter().preflight(_request("AADS", sha)))
    assert good.ok
