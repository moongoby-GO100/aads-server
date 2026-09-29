import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock, Mock

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
    reservation = Mock()
    reservation.__enter__ = Mock(return_value=reservation)
    reservation.__exit__ = Mock(return_value=False)
    reservation.getsockname.return_value = ("127.0.0.1", 19080)
    monkeypatch.setattr(managed.socket, "socket", Mock(return_value=reservation))
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


@pytest.mark.asyncio
async def test_egress_ip_uses_browser_context_and_closes_probe_page():
    page = AsyncMock()
    page.locator = Mock()
    page.locator.return_value.inner_text = AsyncMock(return_value="203.0.113.9")
    page.goto.return_value.status = 200
    context = AsyncMock()
    context.new_page.return_value = page
    assert await managed.measure_browser_egress_ip(context) == "203.0.113.9"
    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_egress_ip_probe_failure_is_explicit():
    page = AsyncMock()
    page.locator = Mock()
    page.locator.return_value.inner_text = AsyncMock(return_value="unverified")
    page.goto.return_value.status = 200
    context = AsyncMock()
    context.new_page.return_value = page
    with pytest.raises(RuntimeError, match="egress_ip_probe_failed"):
        await managed.measure_browser_egress_ip(context)
    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_access_result_reports_lane_ip_and_unverified_authentication(monkeypatch):
    monkeypatch.setattr(gateway, "_probe_self_hosted_playwright_access", AsyncMock(return_value={
        "status": "reachable", "egress_ip": "203.0.113.9",
        "access_diagnosis": {"category": "auth_required", "approval_required": True},
    }))
    result = await gateway.check_browser_target_access(
        work_key="aads", target_url="https://example.com/login", egress_policy="auto"
    )
    assert result["egress_requested"] == "auto"
    assert result["egress_effective"] == "direct"
    assert result["egress_ip"] == "203.0.113.9"
    assert result["authentication_status"] == "unverified"
    assert result["remediation"]["next_action"] == "vault_login_or_approval"


@pytest.mark.asyncio
async def test_ip_probe_failure_preserves_open_page_diagnosis(monkeypatch):
    page = Mock()
    page.context = Mock()
    page.url = "https://store.coupangeats.com/merchant/login"
    page.title = AsyncMock(return_value="Login")
    page.locator.return_value.inner_text = AsyncMock(return_value="CAPTCHA required")

    @asynccontextmanager
    async def open_page(*_args):
        yield page, SimpleNamespace(status=200)

    @asynccontextmanager
    async def fake_playwright():
        yield Mock()

    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=fake_playwright))
    monkeypatch.setattr(gateway, "_open_self_hosted_page", open_page)
    monkeypatch.setattr(gateway, "measure_browser_egress_ip", AsyncMock(side_effect=RuntimeError("probe error")))
    result = await gateway.check_browser_target_access(
        work_key="aads", target_url=page.url, egress_policy="direct"
    )
    assert result["status"] == "reachable"
    assert result["diagnosis"]["reason_code"] == "captcha_detected"
    assert result["egress_ip"] is None
    assert result["egress_failure_reason"] == "egress_ip_probe_failed"
    assert result["authentication_status"] == "unverified"
