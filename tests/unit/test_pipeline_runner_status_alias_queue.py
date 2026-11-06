"""서버 원장 ID 정규화 및 대기 단일 배분 회귀 검증."""
import pytest

from tests.unit.test_pipeline_runner_status_queued import _host, _status


@pytest.mark.asyncio
async def test_aliases_merge_and_queue_is_counted_once(monkeypatch):
    rows = [dict(runner_host=host, project="AADS", status="running", cnt=1, oldest_sec=30)
            for host in ("vmi3267555", "")]
    result = await _status(monkeypatch, rows,
                           [dict(project="GO100", cnt=2), dict(project="AADS", cnt=1)],
                           [_host("vmi3267555", "AADS", 4),
                            _host("contabo42", "AADS", 15),
                            _host("contabo92", "GO100", 12),
                            _host("contabo14", "GO100", 3),
                            _host("rfree-0009", "SF,NTV2,NAS,ACCT", 10),
                            _host("rfree-0009.cafe24.com", "SF", 10)])
    servers = {s["host"]: s for s in result["servers"]}
    assert len(servers) == 5
    assert servers["contabo116"]["running"] == 2
    assert servers["contabo116"]["label"] == "contabo116 (AADS 보조)"
    assert servers["contabo116"]["aliases"] == ["contabo116", "vmi3267555"]
    assert servers["cafe24_114"]["aliases"] == ["rfree-0009", "rfree-0009.cafe24.com"]
    assert servers["contabo92"]["queued"] == 2
    assert servers["contabo14"]["queued"] == 0
    assert servers["contabo42"]["queued"] == 1
    assert sum(s["queued"] for s in servers.values()) == result["totals"]["queued"]
    assert all(s["label"] and s["aliases"] for s in servers.values())


@pytest.mark.asyncio
async def test_duplicate_heartbeat_uses_freshest_and_unions_projects(monkeypatch):
    stale = _host("contabo116", "AADS", 2)
    stale["seen_ago"] = 900
    result = await _status(monkeypatch, [], [dict(project="KIS", cnt=2)],
                           [_host("vmi3267555", "AADS,KIS", 4), stale])
    assert len(result["servers"]) == 1
    server = result["servers"][0]
    assert server["alive"] is True
    assert server["max_concurrent"] == 4
    assert server["projects"] == ["AADS", "KIS"]
    assert server["queued"] == 2
    assert server["queued_shared"] is False


@pytest.mark.asyncio
async def test_explicit_queue_host_keeps_assignment(monkeypatch):
    result = await _status(monkeypatch, [],
                           [dict(project="GO100", runner_host="contabo14", cnt=1),
                            dict(project="GO100", runner_host="", cnt=2),
                            dict(project="AADS", runner_host="vmi3267555", cnt=1)], [])
    servers = {s["host"]: s for s in result["servers"]}
    assert servers["contabo14"]["queued"] == 1
    assert servers["contabo92"]["queued"] == 2
    assert servers["contabo116"]["queued"] == 1
    assert result["queued_by_project"] == {"GO100": 3, "AADS": 1}
    assert sum(s["queued"] for s in servers.values()) == result["totals"]["queued"] == 4
