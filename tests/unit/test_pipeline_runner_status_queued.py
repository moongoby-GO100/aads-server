"""러너 현황의 테넌트 대기 집계와 서버 공유 표시 회귀 검증."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.pipeline_runner import runner_status


async def _status(monkeypatch, rows, queued, hosts):
    conn = MagicMock()

    async def fetch(query, *args):
        if "FROM pipeline_runner_hosts" in query:
            return hosts
        assert args[0] == "tenant-test"
        if "GROUP BY project" in query:
            assert "status = 'queued'" in query
            assert "updated_at" not in query
            return queued
        assert "status <> 'queued'" in query
        assert args[1] == 1
        return rows

    conn.fetch = AsyncMock(side_effect=fetch)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    return await runner_status(window_hours=1, context={"tenant": {"id": "tenant-test"}})


def _host(host, projects, limit):
    return dict(host=host, projects=projects, max_concurrent=limit,
                engine_mode="worktree", seen_ago=10)


@pytest.mark.asyncio
async def test_queued_shared_projects_and_preserved_statuses(monkeypatch):
    rows = [dict(runner_host="contabo14", project="GO100", status=status,
                 cnt=1, oldest_sec=42) for status in
            ("running", "claimed", "done", "failed", "cancelled", "awaiting_approval")]
    result = await _status(monkeypatch, rows,
                           [dict(project="GO100", cnt=2), dict(project="AADS", cnt=1)],
                           [_host("contabo14", " GO100,GO100 ", 3),
                            _host("contabo92", "GO100", 1),
                            _host("vmi3267555", "AADS", 2)])
    servers = {s["host"]: s for s in result["servers"]}
    assert result["queued_by_project"] == {"GO100": 2, "AADS": 1}
    assert result["totals"] == dict(hosts=3, queued=3, running=2, error=1, done=1, cancelled=1)
    for host in ("contabo14", "contabo92"):
        assert servers[host]["queued"] == 2
        assert servers[host]["queued_shared"] is True
    assert servers["vmi3267555"]["queued"] == 1
    assert servers["vmi3267555"]["queued_shared"] is False
    assert [servers[h]["other"] for h in servers] == [1, 0, 0]
    assert servers["contabo14"]["max_concurrent"] == 3
    assert servers["contabo92"]["max_concurrent"] == 1
    assert servers["contabo14"]["oldest_running_sec"] == 42
    assert servers["contabo14"]["alive"] is True


@pytest.mark.asyncio
async def test_queued_never_uses_historical_project_fallback(monkeypatch):
    result = await _status(monkeypatch,
                           [dict(runner_host="", project="AADS", status="done", cnt=1, oldest_sec=None)],
                           [dict(project="AADS", cnt=1), dict(project="UNASSIGNED", cnt=2)],
                           [_host("contabo92", "GO100", 1)])
    servers = {s["host"]: s for s in result["servers"]}
    assert result["totals"]["queued"] == 3
    assert servers["contabo116"]["done"] == 1
    assert servers["contabo116"]["max_concurrent"] is None
    assert all(s["queued"] == 0 and s["other"] == 0 for s in servers.values())
    assert all(s["queued_shared"] is False for s in servers.values())
    assert "unknown" not in servers


@pytest.mark.asyncio
async def test_empty_status(monkeypatch):
    result = await _status(monkeypatch, [], [], [])
    assert result["servers"] == []
    assert result["queued_by_project"] == {}
    assert result["totals"]["queued"] == 0
