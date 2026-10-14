import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services import managed_browser as managed
from app.services import browser_task_gateway as gateway


@pytest.mark.asyncio
async def test_direct_does_not_start_ssh(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async with managed.browser_egress(managed.egress_for_target("direct", "https://example.com")) as proxy:
        assert proxy is None
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_tunnel_failure_does_not_yield_direct(monkeypatch):
    monkeypatch.delenv("CAFE24_EGRESS_PROXY_URL", raising=False)
    process = AsyncMock(returncode=255)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(RuntimeError, match="cafe24_ssh_tunnel_failed"):
        async with managed.browser_egress(managed.egress_for_target("auto", "https://store.coupangeats.com")):
            pytest.fail("failed tunnel must not start the browser")


@pytest.mark.asyncio
async def test_cafe24_capture_failure_never_switches_to_pc(monkeypatch):
    monkeypatch.setattr(gateway, "get_browser_task", AsyncMock(return_value={"id": "task", "work_key": "work"}))
    monkeypatch.setattr(gateway, "_capture_self_hosted_playwright_frame", AsyncMock(return_value={
        "status": "skipped", "egress_effective": "cafe24", "reason": "tunnel_failed",
    }))
    monkeypatch.setattr(gateway, "record_browser_task_step", AsyncMock())
    pc = AsyncMock()
    monkeypatch.setattr(gateway, "_capture_pc_agent_frame", pc)
    await gateway.capture_browser_task_live_frame(tenant_id="tenant", task_id="task")
    pc.assert_not_called()


def test_capture_profiles_are_tenant_and_session_scoped():
    first, _, _ = gateway._self_hosted_capture_settings({"tenant_id": "a", "session_id": "s", "target_url": "https://example.com", "work_key": "shared"})
    second, _, _ = gateway._self_hosted_capture_settings({"tenant_id": "b", "session_id": "s", "target_url": "https://example.com", "work_key": "shared"})
    assert first["profile_dir"] != second["profile_dir"]


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:bad", "http://user:pass@127.0.0.1:1234", "http://example.com:1234"])
def test_invalid_proxy_configuration_fails_closed(monkeypatch, endpoint):
    monkeypatch.setenv("CAFE24_EGRESS_PROXY_VAULT_REF", "vault://test")
    monkeypatch.setenv("CAFE24_EGRESS_PROXY_URL", endpoint)
    assert managed.egress_for_target("cafe24", "https://example.com")["egress_effective"] == "unavailable"


def test_lane_report_failed_cafe24_is_a_failure_with_next_steps_and_no_direct_switch():
    egress = {k: v for k, v in managed.egress_for_target("cafe24", "https://example.com").items() if k != "proxy"}
    lane = gateway.build_lane_report({"status": "skipped", "reason": "tunnel_failed", **egress})
    assert lane["lane_requested"] == "cafe24"
    assert lane["lane_effective"] in {"cafe24", "unavailable"}
    assert lane["lane_ok"] is False
    assert lane["lane_failure_reason"]
    assert lane["lane_next_steps"]
    assert "direct" not in lane["lane_effective"]
    assert lane["login_verified"] is False


def test_lane_report_reachable_normal_page_is_ok_but_not_a_login():
    diagnosis = gateway.classify_playwright_access(status="reachable", http_status=200, body_text="Welcome to the dashboard")
    lane = gateway.build_lane_report(
        {"status": "reachable", "egress_requested": "auto", "egress_effective": "direct", "access_diagnosis": diagnosis},
        egress_ip="203.0.113.7", egress_ip_status="observed",
    )
    assert (lane["lane_requested"], lane["lane_effective"]) == ("auto", "direct")
    assert lane["lane_ok"] is True and lane["lane_failure_reason"] == ""
    assert lane["egress_ip"] == "203.0.113.7"
    assert lane["login_verified"] is False


def test_remediation_keeps_existing_next_action_and_pc_agent_runtimes():
    plan = gateway.build_access_remediation_plan({"category": "bot_or_waf_blocked"})
    assert plan["next_action"] == "switch_runtime"
    assert "pc_agent" in {plan["primary_runtime"], *plan["fallback_runtimes"]}
    assert gateway.build_access_remediation_plan({"category": "reachable"})["fallback_runtimes"] == ["pc_agent"]


@pytest.mark.asyncio
async def test_cafe24_capture_failure_reports_lane_and_never_switches_to_pc(monkeypatch):
    monkeypatch.setattr(gateway, "get_browser_task", AsyncMock(return_value={"id": "task", "work_key": "work", "egress_policy": "cafe24"}))
    monkeypatch.setattr(gateway, "_capture_self_hosted_playwright_frame", AsyncMock(return_value={
        "status": "skipped", "egress_requested": "cafe24", "egress_effective": "cafe24", "egress_reason": "cafe24_ssh_tunnel_failed",
    }))
    monkeypatch.setattr(gateway, "record_browser_task_step", AsyncMock())
    pc = AsyncMock()
    monkeypatch.setattr(gateway, "_capture_pc_agent_frame", pc)
    result = await gateway.capture_browser_task_live_frame(tenant_id="tenant", task_id="task")
    pc.assert_not_called()
    assert result["lane"]["lane_ok"] is False
    assert result["lane"]["lane_failure_reason"] == "cafe24_ssh_tunnel_failed"
    assert result["lane"]["lane_requested"] == "cafe24"


class _EchoPage:
    def __init__(self, body=None, fail=False):
        self.body, self.fail, self.closed = body, fail, False
        self.context = self

    async def new_page(self):
        return self

    async def goto(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")

    def locator(self, selector):
        page = self

        class _Loc:
            async def inner_text(self, timeout=0):
                return page.body

        return _Loc()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_observe_egress_ip_accepts_only_a_real_ip_and_closes_probe_page():
    page = _EchoPage('{"ip": "203.0.113.7"}')
    assert await gateway._observe_egress_ip(page) == ("203.0.113.7", "observed")
    assert page.closed
    assert await gateway._observe_egress_ip(_EchoPage('{"ip": "not-an-ip"}')) == (None, "lookup_failed")
    assert await gateway._observe_egress_ip(_EchoPage(fail=True)) == (None, "lookup_failed")
