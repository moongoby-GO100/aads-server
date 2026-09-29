"""Dedicated TCP reachability monitoring for the ACCT SSH tunnel host."""

import asyncio

from app.services import health_checker


class _Writer:
    def close(self):
        pass

    async def wait_closed(self):
        pass


def test_tcp_probe_success(monkeypatch):
    async def open_connection(host, port):
        assert host == "5.104.85.244"
        assert port == 22
        return object(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = asyncio.run(health_checker._check_tcp_reachable("5.104.85.244", 22))

    assert result["ok"] is True
    assert result["port_open"] is True
    assert "latency_ms" in result


def test_tcp_probe_refused_is_warning(monkeypatch):
    async def open_connection(host, port):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = asyncio.run(health_checker._check_tcp_reachable("5.104.85.244", 22))

    assert result["ok"] is False
    assert result["reason"] == "refused"
    assert result["severity"] == "warning"
    assert result["error"] == "refused"


def test_tcp_probe_timeout_is_warning(monkeypatch):
    async def open_connection(host, port):
        await asyncio.sleep(1)

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = asyncio.run(health_checker._check_tcp_reachable("5.104.85.244", 22, timeout=0.001))

    assert result["ok"] is False
    assert result["reason"] == "timeout"
    assert result["severity"] == "warning"


def test_infra_probe_failure_degrades_without_critical_and_preserves_keys(monkeypatch):
    async def healthy():
        return {"ok": True}

    async def healthy_ssh(_):
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

    monkeypatch.setattr(health_checker, "_check_db", healthy)
    monkeypatch.setattr(health_checker, "_check_github_pat", healthy)
    monkeypatch.setattr(health_checker, "_check_ssh", healthy_ssh)
    monkeypatch.setattr(health_checker, "_check_disk", healthy_disk)
    monkeypatch.setattr(health_checker, "_check_memory", healthy)
    monkeypatch.setattr(health_checker, "_check_cpu", healthy)
    monkeypatch.setattr(health_checker, "_check_tcp_reachable", failed_tcp)

    result = asyncio.run(health_checker.check_infra())
    expected = [
        "db", "github_pat", "ssh_211", "ssh_114", "disk_68", "disk_211",
        "disk_114", "memory_68", "cpu_68",
    ]

    assert result["ssh_port_jinah244"]["reason"] == "refused"
    assert result["overall"] == "DEGRADED"
    assert result["overall"] != "CRITICAL"
    assert all(key in result for key in expected)


def test_alert_manager_evaluates_jinah244_probe(monkeypatch):
    from app.services.alert_manager import AlertManager

    async def metrics(_self):
        return {}

    async def infra():
        return {
            "ssh_port_jinah244": {
                "ok": False, "reason": "refused", "error": "connection refused",
                "latency_ms": 2, "severity": "warning",
            }
        }

    monkeypatch.setattr(AlertManager, "_collect_metrics", metrics)
    monkeypatch.setattr(health_checker, "check_infra", infra)
    alerts = asyncio.run(AlertManager().evaluate_rules())

    alert = next(a for a in alerts if a.category == "jinah244_ssh_port_unreachable")
    assert alert.severity == "WARNING"
    assert "refused" in alert.message
