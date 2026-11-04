"""contabo92 건강감시: 원장 표시(4대), check_infra 합류, 경보, 원격 timeout 래핑."""
import asyncio

from app.services import health_checker as hc
from app.services.server_registry import get_server_config

CFG = get_server_config("contabo92")["health_probe"]

GOOD = (
    "SSH=ok\nDISK=7% 790000000 810000000\nPG_SERVICE=active\nDB=1\n"
    "LOAD=0.50 0.40 0.30\nNPROC=12\nMEM=48000 40000\nEND=ok\n"
)


def test_ledger_prompt_counts_four():
    from app.services.context_builder import build_server_ledger_snapshot  # noqa
    snap = build_server_ledger_snapshot()
    assert snap["health_monitored_count"] == 4
    row = [s for s in snap["servers"] if s["server_id"] == "contabo92"][0]
    assert row["health_monitored"] is True


def test_parse_ok():
    r = hc._parse_probe92_output(GOOD, CFG)
    assert r["ssh"]["ok"] and r["disk"]["ok"] and r["postgres"]["ok"] and r["load"]["ok"]
    assert r["disk"]["usage_pct"] == 7 and r["load"]["cores"] == 12
    assert "severity" not in r["disk"]


def test_disk_thresholds():
    warn = hc._parse_probe92_output(GOOD.replace("7%", "85%"), CFG)["disk"]
    crit = hc._parse_probe92_output(GOOD.replace("7%", "91%"), CFG)["disk"]
    assert not warn["ok"] and warn["severity"] == "warning"
    assert not crit["ok"] and crit["severity"] == "critical"


def test_postgres_down_and_ssh_down():
    pg = hc._parse_probe92_output(GOOD.replace("PG_SERVICE=active", "PG_SERVICE=inactive"), CFG)["postgres"]
    assert not pg["ok"] and pg["severity"] == "critical"
    dead = hc._parse_probe92_output("", CFG)
    assert not dead["ssh"]["ok"] and dead["ssh"]["severity"] == "warning"
    assert dead["postgres"]["severity"] == "warning"


def test_script_wraps_remote_commands_with_timeout_and_readonly():
    script = hc._build_probe92_script("go100_history")
    for cmd in ("df -P", "systemctl is-active", "psql", "free -m"):
        idx = script.index(cmd)
        assert "timeout " in script[max(0, idx - 80):idx]
    assert "default_transaction_read_only=on" in script
    assert "SELECT 1" in script


def test_check_contabo92_uses_hop_with_timeouts(monkeypatch):
    seen = {}

    async def fake(cmd, timeout=5.0):
        seen["cmd"], seen["timeout"] = cmd, timeout
        return GOOD

    monkeypatch.setattr(hc, "_run_local_cmd", fake)
    r = asyncio.run(hc._check_contabo92())
    assert r["disk"]["usage_pct"] == 7
    assert "mesh-92" in seen["cmd"] and "5.104.86.14" in seen["cmd"]
    assert seen["cmd"].count("timeout ") >= 6


def test_check_infra_includes_92(monkeypatch):
    async def ok(*a, **k):
        return {"ok": True}

    async def p92():
        return hc._parse_probe92_output(GOOD.replace("7%", "93%"), CFG)

    for n in ("_check_db", "_check_github_pat", "_check_ssh", "_check_disk",
              "_check_memory", "_check_cpu"):
        monkeypatch.setattr(hc, n, ok)
    monkeypatch.setattr(hc, "_check_contabo92", p92)
    infra = asyncio.run(hc.check_infra())
    assert infra["disk_92"]["usage_pct"] == 93
    assert infra["overall"] == "CRITICAL"
    assert any(i["type"].startswith("disk_92") for i in infra["issues"])


def test_alert_manager_contabo92_disk(monkeypatch):
    from app.services.alert_manager import AlertManager
    am = AlertManager.__new__(AlertManager)

    async def metrics():
        return {"contabo92_disk_usage_percent": 91.0}

    am._collect_metrics = metrics
    alerts = asyncio.run(am.evaluate_rules())
    a = [x for x in alerts if x.server == "contabo92"]
    assert a and a[0].severity == "CRITICAL" and a[0].category == "disk_full"


def test_check_infra_warning_disk_is_degraded(monkeypatch):
    async def ok(*a, **k):
        return {"ok": True}

    async def p92():
        return hc._parse_probe92_output(GOOD.replace("7%", "85%"), CFG)

    for n in ("_check_db", "_check_github_pat", "_check_ssh", "_check_disk",
              "_check_memory", "_check_cpu"):
        monkeypatch.setattr(hc, n, ok)
    monkeypatch.setattr(hc, "_check_contabo92", p92)
    infra = asyncio.run(hc.check_infra())
    assert infra["overall"] == "DEGRADED"
    assert any(i["type"] == "disk_92_warning" for i in infra["issues"])


def test_hop_ssh_does_not_auto_accept_host_keys(monkeypatch):
    seen = {}

    async def fake(cmd, timeout=5.0):
        seen["cmd"] = cmd
        return GOOD

    monkeypatch.setattr(hc, "_run_local_cmd", fake)
    asyncio.run(hc._check_contabo92())
    assert "StrictHostKeyChecking=yes" in seen["cmd"]
    assert "StrictHostKeyChecking=no" not in seen["cmd"]


def _eval(metrics_dict):
    from app.services.alert_manager import AlertManager
    am = AlertManager.__new__(AlertManager)

    async def metrics():
        return metrics_dict

    am._collect_metrics = metrics
    return [x for x in asyncio.run(am.evaluate_rules()) if x.server == "contabo92"]


def test_alert_warning_and_critical_use_distinct_categories():
    w = _eval({"contabo92_disk_usage_percent": 85.0})
    c = _eval({"contabo92_disk_usage_percent": 91.0})
    assert w[0].severity == "WARNING" and c[0].severity == "CRITICAL"
    assert w[0].category != c[0].category


def test_alert_ssh_and_pg_failures():
    s = _eval({"contabo92_ssh_ok": False, "contabo92_ssh_error": "refused"})
    assert s and s[0].category == "ssh_timeout"
    p = _eval({"contabo92_ssh_ok": True, "contabo92_pg_ok": False, "contabo92_pg_error": "x"})
    assert p and p[0].severity == "CRITICAL" and p[0].category == "health_fail"
    assert _eval({"contabo92_ssh_ok": True, "contabo92_pg_ok": True}) == []


def test_collect_metrics_carries_ssh_and_pg(monkeypatch):
    from app.services.alert_manager import AlertManager

    async def p92():
        return hc._parse_probe92_output(GOOD.replace("DB=1", "DB=err"), CFG)

    monkeypatch.setattr(hc, "_check_contabo92", p92)
    m = asyncio.run(AlertManager.__new__(AlertManager)._collect_metrics())
    assert m["contabo92_ssh_ok"] is True and m["contabo92_pg_ok"] is False
    assert m["contabo92_disk_usage_percent"] == 7.0


# ── 리뷰 3차 교정 ────────────────────────────────────────────────────────────
def _infra_with(monkeypatch, probe):
    async def ok(*a, **k):
        return {"ok": True}

    async def p92():
        return probe

    for n in ("_check_db", "_check_github_pat", "_check_ssh", "_check_disk",
              "_check_memory", "_check_cpu"):
        monkeypatch.setattr(hc, n, ok)
    monkeypatch.setattr(hc, "_check_contabo92", p92)
    return asyncio.run(hc.check_infra())


def test_probe92_time_budget_inequality():
    step_sum = hc._PROBE92_STEP_TIMEOUT * hc._PROBE92_STEPS
    assert step_sum + hc._PROBE92_CONNECT_TIMEOUT < hc._PROBE92_INNER_TIMEOUT
    assert hc._PROBE92_INNER_TIMEOUT < hc._PROBE92_HOP_REMOTE_TIMEOUT
    assert hc._PROBE92_HOP_REMOTE_TIMEOUT + hc._PROBE92_CONNECT_TIMEOUT < hc._PROBE92_HOP_TIMEOUT
    # 스크립트의 timeout 단계 수가 상수와 일치해야 부등식이 의미를 갖는다
    script = hc._build_probe92_script("go100_history")
    assert script.count(f"timeout {hc._PROBE92_STEP_TIMEOUT} ") == hc._PROBE92_STEPS


def test_probe92_script_ends_with_end_marker():
    assert hc._build_probe92_script("go100_history").rstrip().endswith("echo END=ok")


def test_truncated_output_is_warning_not_critical():
    r = hc._parse_probe92_output("SSH=ok\nDISK=7% 790000000 810000000\n", CFG)
    assert r["complete"] is False
    assert r["ssh"]["ok"]
    assert r["postgres"]["severity"] == "warning"
    assert r["load"]["severity"] == "warning"
    assert r["disk"]["ok"]


def test_complete_output_marks_complete_and_pg_down_critical():
    assert hc._parse_probe92_output(GOOD, CFG)["complete"] is True
    bad = hc._parse_probe92_output(GOOD.replace("DB=1", "DB=psql: error"), CFG)
    assert bad["postgres"]["severity"] == "critical"


def test_step_timeout_empty_values_are_warning():
    out = GOOD.replace("PG_SERVICE=active", "PG_SERVICE=").replace("DB=1", "DB=")
    assert hc._parse_probe92_output(out, CFG)["postgres"]["severity"] == "warning"


def test_ssh_failure_all_items_warning():
    r = hc._parse_probe92_output("", CFG)
    for k in ("ssh", "disk", "postgres", "load"):
        assert r[k]["severity"] == "warning", k


def test_check_infra_ssh_failure_alone_is_degraded(monkeypatch):
    infra = _infra_with(monkeypatch, hc._parse_probe92_output("", CFG))
    assert infra["overall"] == "DEGRADED"
    assert not any(i["severity"] == "critical" for i in infra["issues"])


def test_check_infra_disk_91_critical_and_85_degraded(monkeypatch):
    c = _infra_with(monkeypatch, hc._parse_probe92_output(GOOD.replace("7%", "91%"), CFG))
    w = _infra_with(monkeypatch, hc._parse_probe92_output(GOOD.replace("7%", "85%"), CFG))
    assert c["overall"] == "CRITICAL"
    assert w["overall"] == "DEGRADED"


def test_check_infra_runs_probe_in_parallel(monkeypatch):
    import time

    async def slow(*a, **k):
        await asyncio.sleep(0.3)
        return {"ok": True}

    async def p92():
        await asyncio.sleep(0.3)
        return hc._parse_probe92_output(GOOD, CFG)

    for n in ("_check_db", "_check_github_pat", "_check_ssh", "_check_disk",
              "_check_memory", "_check_cpu"):
        monkeypatch.setattr(hc, n, slow)
    monkeypatch.setattr(hc, "_check_contabo92", p92)
    t0 = time.monotonic()
    infra = asyncio.run(hc.check_infra())
    assert time.monotonic() - t0 < 0.55
    assert infra["overall"] == "HEALTHY"


def test_both_hops_enforce_strict_host_key_checking(monkeypatch):
    seen = {}

    async def fake(cmd, timeout=5.0):
        seen["cmd"], seen["timeout"] = cmd, timeout
        return GOOD

    monkeypatch.setattr(hc, "_run_local_cmd", fake)
    asyncio.run(hc._check_contabo92())
    assert seen["cmd"].count("StrictHostKeyChecking=yes") == 2
    assert "StrictHostKeyChecking=no" not in seen["cmd"]
    assert "accept-new" not in seen["cmd"]
    assert seen["timeout"] == hc._PROBE92_HOP_TIMEOUT
