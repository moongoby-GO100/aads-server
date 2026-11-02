"""Retired host monitoring is disabled; generic TCP and active alerts remain."""

import asyncio
import socket
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import health_checker


class _Writer:
    def close(self):
        pass

    async def wait_closed(self):
        pass


def test_tcp_probe_success(monkeypatch):
    async def open_connection(host, port):
        assert host == "192.0.2.1"
        assert port == 22
        return object(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = asyncio.run(health_checker._check_tcp_reachable("192.0.2.1", 22))

    assert result["ok"] is True
    assert result["port_open"] is True
    assert "latency_ms" in result


def test_tcp_probe_refused_is_warning(monkeypatch):
    async def open_connection(host, port):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = asyncio.run(health_checker._check_tcp_reachable("192.0.2.1", 22))

    assert result["ok"] is False
    assert result["reason"] == "refused"
    assert result["severity"] == "warning"
    assert result["error"] == "refused"


def test_tcp_probe_timeout_is_warning(monkeypatch):
    async def open_connection(host, port):
        await asyncio.sleep(1)

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = asyncio.run(health_checker._check_tcp_reachable("192.0.2.1", 22, timeout=0.001))

    assert result["ok"] is False
    assert result["reason"] == "timeout"
    assert result["severity"] == "warning"


@pytest.mark.parametrize("status", ["HEALTHY", "DEGRADED", "CRITICAL"])
def test_infra_probe_failure_degrades_without_critical_and_preserves_keys(monkeypatch, status):
    async def healthy():
        return {"ok": True}

    ssh_calls = []

    async def healthy_ssh(server):
        ssh_calls.append(server)
        return {"ok": True}

    async def healthy_disk(*_):
        return {"ok": True}

    async def failed_tcp(*_args, **_kwargs):
        return {
            "ok": False,
            "port_open": False,
            "reason": "refused",
            "error": "connection refused",
            "severity": "warning",
        }

    async def db_status():
        return {"ok": status == "HEALTHY", "severity": "warning" if status == "DEGRADED" else "critical"}

    tcp = AsyncMock(side_effect=failed_tcp)
    forbidden = Mock(side_effect=AssertionError("Unexpected host resolution or network call"))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    monkeypatch.setattr(health_checker, "get_server_config", forbidden)
    monkeypatch.setattr(health_checker, "_check_db", db_status)
    monkeypatch.setattr(health_checker, "_check_github_pat", healthy)
    monkeypatch.setattr(health_checker, "_check_ssh", healthy_ssh)
    monkeypatch.setattr(health_checker, "_check_disk", healthy_disk)
    monkeypatch.setattr(health_checker, "_check_memory", healthy)
    monkeypatch.setattr(health_checker, "_check_cpu", healthy)
    monkeypatch.setattr(health_checker, "_check_tcp_reachable", tcp)

    result = asyncio.run(health_checker.check_infra())
    expected = [
        "db", "github_pat", "ssh_211", "ssh_114", "disk_68", "disk_211",
        "disk_114", "memory_68", "cpu_68",
    ]

    tcp.assert_not_called()
    forbidden.assert_not_called()
    assert ssh_calls == ["211", "114"]
    assert "ssh_port_jinah244" not in result
    assert result["overall"] == status
    assert set(result) == set(expected) | {"overall", "issues"}
    assert len(result["issues"]) == (status != "HEALTHY")

    # Reuse the failure fixture on an active check to verify critical propagation.
    monkeypatch.setattr(health_checker, "_check_cpu", failed_tcp)
    result = asyncio.run(health_checker.check_infra())
    assert result["cpu_68"]["reason"] == "refused"
    assert result["overall"] == ("CRITICAL" if status == "CRITICAL" else "DEGRADED")
    tcp.assert_not_called()


def test_alert_manager_evaluates_jinah244_probe(monkeypatch):
    from app.services.alert_manager import AlertManager

    async def metrics(_self):
        return {"disk_usage_percent": 90, "daily_cost_usd": 6,
                "stall_task_count": 1, "memory_usage_percent": 90,
                "github_pat_expires_in_days": 5}

    async def infra():
        return {
            "ssh_port_jinah244": {
                "ok": False, "reason": "refused", "error": "connection refused",
                "latency_ms": 2, "severity": "warning",
            }
        }

    monkeypatch.setattr(AlertManager, "_collect_metrics", metrics)
    infra_mock = AsyncMock(side_effect=infra)
    monkeypatch.setattr(health_checker, "check_infra", infra_mock)
    alerts = asyncio.run(AlertManager().evaluate_rules())

    infra_mock.assert_not_called()
    assert {a.category: a.severity for a in alerts} == {
        "disk_full": "CRITICAL", "cost_exceed": "WARNING", "task_stall": "WARNING",
        "memory_high": "WARNING", "pat_expiry": "INFO",
    }


def test_lease_defaults_only_include_active_targets():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    defaults = {
        "scripts/claude-lease-push.sh": ['TARGETS="${CLAUDE_LEASE_TARGETS:-root@5.104.86.14 root@114.207.244.86}"'],
        "scripts/slot_lease_push.sh": ['TARGETS="${SLOT_LEASE_TARGETS:-5.104.86.14 114.207.244.86}"'],
        "scripts/aads-claude-lease-push.service": [
            'Environment="CLAUDE_LEASE_TARGETS=root@5.104.86.14 root@114.207.244.86"',
            'Environment="SLOT_LEASE_TARGETS=5.104.86.14 114.207.244.86"',
        ],
    }
    for filename, lines in defaults.items():
        content = (root / filename).read_text()
        assert "5.104.85.244" not in content
        for line in lines:
            assert line in content


@pytest.mark.parametrize("module_name", ["health_checker", "alert_manager"])
def test_release_monitoring_sources_exclude_retired_probe(module_name):
    """Check release sources as well as behavior, including dormant alert paths."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "app" / "services" / f"{module_name}.py").read_text(
        encoding="utf-8"
    )
    for retired_marker in (
        "ssh_port_jinah244",
        "jinah244_ssh_port_unreachable",
        "5.104.85.244",
    ):
        assert retired_marker not in source, f"{module_name}: {retired_marker}"
