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
