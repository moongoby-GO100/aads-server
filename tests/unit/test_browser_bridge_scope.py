"""Scope is carried through recovery and cannot be adopted by remote input."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.api.browser_bridge import EnsurePcCdpSessionRequest, ensure_pc_cdp_session
from app.browser_bridge.registry import PairingManager, SessionRegistry
from app.browser_bridge.storage_state import StorageStateManager
from app.browser_bridge.service import BrowserBridgeService, _LocalAgentPage


@pytest.mark.asyncio
async def test_public_legacy_adoption_is_rejected_before_routing():
    with pytest.raises(HTTPException) as exc:
        await ensure_pc_cdp_session(EnsurePcCdpSessionRequest(adopt_legacy_scope=True),
            {"user_id": "other", "tenant_id": "foreign", "is_admin": True})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_scope_survives_every_launch_fallback(monkeypatch, tmp_path):
    service = BrowserBridgeService(pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"), storage_states=StorageStateManager(tmp_path))
    monkeypatch.setattr(service, "_route_pc_agent_via_active_api_first", lambda: True)
    calls = []
    async def route(**kwargs):
        name, params = kwargs["command_type"], kwargs["params"]
        calls.append(name)
        assert params["tenant_id"] == "tenant-a"
        assert params["chat_session_id"] == "chat-a"
        if name in {"browser_launch", "browser_health"}:
            return {"status": "error", "error_code": "CDP_NOT_READY"}
        data = {"port": 9333, "tabs": [{"id": "tab", "url": "about:blank"}]}
        return {"status": "success", "lease": {"agent_id": "pc"}, "result": {"result": data}}
    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", route)
    session = await service.ensure_pc_agent_cdp_session(agent_id="pc", work_key="bank",
        url="https://example.com", preferred_port=9333, tenant_id="tenant-a", chat_session_id="chat-a")
    assert calls == ["browser_launch", "browser_health", "browser_tabs", "browser_navigate"]
    assert session.endpoint.metadata["tenant_id"] == "tenant-a"
    page = _LocalAgentPage(session, service)
    assert page._params({"tenant_id": "forged"})["tenant_id"] == "tenant-a"
    with pytest.raises(ValueError, match="CDP_SCOPE_MISMATCH"):
        await service.ensure_work_session(work_key="bank", tenant_id="tenant-b", chat_session_id="chat-a")


@pytest.mark.asyncio
async def test_chat_id_must_belong_to_authenticated_user(monkeypatch):
    import app.core.db_pool as db_pool
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=None))
    class Acquire:
        async def __aenter__(self): return conn
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(db_pool, "get_pool", lambda: SimpleNamespace(acquire=lambda: Acquire()))
    with pytest.raises(HTTPException) as exc:
        await ensure_pc_cdp_session(EnsurePcCdpSessionRequest(chat_session_id="00000000-0000-0000-0000-000000000001"),
            {"user_id": "foreign", "tenant_id": "00000000-0000-0000-0000-000000000002"})
    assert exc.value.status_code == 403
    assert conn.fetchval.call_args.args[-1] == "foreign"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["ensure", "execute"])
async def test_work_endpoints_forward_verified_scope_and_pin_route(monkeypatch, entry):
    from app.api import browser_bridge as api
    scope = {"tenant_id": "tenant-a", "chat_session_id": "chat-a"}
    verify = AsyncMock(return_value=scope)
    monkeypatch.setattr(api, "_verified_browser_scope", verify)
    session = SimpleNamespace(work_key="bank", session_id="bridge-a", label="bank",
        endpoint=SimpleNamespace(public_dict=lambda: {"metadata": {"port": "9333", "agent_id": "pc"}}),
        public_dict=lambda: {})
    service = SimpleNamespace(ensure_work_session=AsyncMock(return_value=session),
        _execute_pc_agent_route_via_active_api=AsyncMock(return_value={"status": "success"}))
    monkeypatch.setattr(api, "get_browser_bridge_service", lambda: service)
    current_user = {"user_id": "user-a", "tenant_id": "tenant-a"}
    if entry == "ensure":
        await api.ensure_work_session(api.EnsureWorkSessionRequest(work_key="bank", chat_session_id="chat-a"), current_user)
    else:
        await api.route_execute_work_session(api.WorkSessionRouteExecuteRequest(work_key="bank", chat_session_id="chat-a",
            command_type="browser_tabs", params={"tenant_id": "foreign", "chat_session_id": "foreign",
                "work_key": "foreign", "port": 9444, "preferred_port": 9444, "adopt_legacy_scope": True}), current_user)
        params = service._execute_pc_agent_route_via_active_api.call_args.kwargs["params"]
        assert params["tenant_id"] == "tenant-a"
        assert params["chat_session_id"] == "chat-a"
        assert params["work_key"] == "bank"
        assert params["port"] == params["preferred_port"] == 9333
        assert "adopt_legacy_scope" not in params
    verify.assert_awaited_once_with(current_user, "chat-a")
    assert service.ensure_work_session.call_args.kwargs["tenant_id"] == "tenant-a"
    assert service.ensure_work_session.call_args.kwargs["chat_session_id"] == "chat-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["ensure", "execute"])
async def test_work_endpoints_reject_foreign_chat_before_routing(monkeypatch, entry):
    from app.api import browser_bridge as api
    import app.core.db_pool as db_pool
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=None))
    class Acquire:
        async def __aenter__(self): return conn
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(db_pool, "get_pool", lambda: SimpleNamespace(acquire=lambda: Acquire()))
    args = {"work_key": "bank", "chat_session_id": "00000000-0000-0000-0000-000000000001"}
    current_user = {"user_id": "foreign", "tenant_id": "00000000-0000-0000-0000-000000000002"}
    with pytest.raises(HTTPException) as exc:
        if entry == "ensure":
            await api.ensure_work_session(api.EnsureWorkSessionRequest(**args), current_user)
        else:
            await api.route_execute_work_session(api.WorkSessionRouteExecuteRequest(**args, command_type="browser_tabs"), current_user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_adapter_uses_bound_context_not_stored_scope(monkeypatch):
    from app.browser_bridge import aads_adapter
    from app.services.tool_executor import current_chat_session_id, current_tenant_id
    session = SimpleNamespace(session_id="bridge-a")
    service = SimpleNamespace(ensure_work_session=AsyncMock(return_value=session),
        acquire_playwright_context=AsyncMock(return_value=(object(), None)))
    monkeypatch.setattr(aads_adapter, "get_browser_bridge_service", lambda: service)
    tt, ct = current_tenant_id.set("tenant-a"), current_chat_session_id.set("chat-a")
    try:
        _, err = await aads_adapter.acquire_browser_context(browser_work_key="bank")
        assert err is None
        assert service.ensure_work_session.call_args.kwargs["tenant_id"] == "tenant-a"
        assert service.acquire_playwright_context.call_args.kwargs["chat_session_id"] == "chat-a"
        ctx, err = await aads_adapter.acquire_browser_context(browser_session_id="bridge-a", tenant_id="foreign")
        assert ctx is None and "CDP_SCOPE_MISMATCH" in err
    finally:
        current_tenant_id.reset(tt)
        current_chat_session_id.reset(ct)


@pytest.mark.asyncio
async def test_scoped_context_cannot_be_acquired_by_session_id_alone(monkeypatch, tmp_path):
    service = BrowserBridgeService(pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"), storage_states=StorageStateManager(tmp_path))
    pairing = service.create_pairing()
    session = service.register_session(pairing_token=pairing.token, label="Scoped browser", endpoint_kind="local_agent",
        metadata={"agent_id": "pc", "port": "9333", "tenant_id": "tenant-a", "chat_session_id": "chat-a"})
    context = object()
    acquire = AsyncMock(return_value=context)
    monkeypatch.setattr(service, "_context_for_session", acquire)
    result, err = await service.acquire_playwright_context(session.session_id)
    assert result is None and "CDP_SCOPE_MISMATCH" in err
    acquire.assert_not_called()
    result, err = await service.acquire_playwright_context(session.session_id, tenant_id="tenant-a", chat_session_id="chat-a")
    assert result is context and err is None
