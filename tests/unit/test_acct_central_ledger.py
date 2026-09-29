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


def test_acct_empty_repo_path_blocks_adapter(monkeypatch):
    monkeypatch.setenv("ACCT_REPO_PATH", "")
    details = git_release_preflight("ACCT", "abcdef0")
    assert details["config_error"] == "ACCT_REPO_PATH 미설정"
    class AcctAdapter(BaseDeployAdapter):
        project = "ACCT"

    result = asyncio.run(AcctAdapter().preflight(DeployRequest(project="ACCT", component="api", deploy_type="api_bluegreen", release_sha="abcdef0")))
    assert not result.ok
    assert "ACCT_REPO_PATH 미설정" in result.blockers
