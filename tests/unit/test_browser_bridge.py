from __future__ import annotations

import io
import json
import urllib.error
import builtins

import pytest

from app.browser_bridge import service as service_module
from app.browser_bridge.models import BrowserEndpointKind
from app.browser_bridge.registry import PairingManager, SessionRegistry
from app.browser_bridge.security import BrowserBridgeSecurityError, validate_bridge_endpoint
from app.browser_bridge.service import BrowserBridgeService
from app.browser_bridge.storage_state import StorageStateManager


def test_validate_bridge_endpoint_allows_loopback_cdp() -> None:
    validate_bridge_endpoint(BrowserEndpointKind.CDP, "http://127.0.0.1:9222")
    validate_bridge_endpoint(BrowserEndpointKind.CDP, "ws://localhost:9222/devtools/browser/abc")
    validate_bridge_endpoint(BrowserEndpointKind.CDP, "http://[::1]:9222")


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:9222",
        "http://192.168.0.10:9222",
        "ws://example.com/devtools/browser/abc",
        "http://user:pass@127.0.0.1:9222",
    ],
)
def test_validate_bridge_endpoint_rejects_public_or_credentialed_cdp(url: str) -> None:
    with pytest.raises(BrowserBridgeSecurityError):
        validate_bridge_endpoint(BrowserEndpointKind.CDP, url)


def test_pairing_token_is_one_time_for_session_registration(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    pairing = service.create_pairing(label="CEO Chrome")

    session = service.register_session(
        pairing_token=pairing.token,
        label="CEO Chrome",
        endpoint_kind="cdp",
        endpoint_url="http://127.0.0.1:9222",
    )

    assert session.active is True
    assert session.endpoint.kind == BrowserEndpointKind.CDP
    assert service.active_session().session_id == session.session_id

    with pytest.raises(ValueError, match="already used"):
        service.register_session(
            pairing_token=pairing.token,
            label="duplicate",
            endpoint_kind="cdp",
            endpoint_url="http://127.0.0.1:9222",
        )


def test_invalid_endpoint_does_not_consume_pairing_token(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    pairing = service.create_pairing(label="CEO Chrome")

    with pytest.raises(BrowserBridgeSecurityError):
        service.register_session(
            pairing_token=pairing.token,
            label="bad",
            endpoint_kind="cdp",
            endpoint_url="http://192.168.0.10:9222",
        )

    session = service.register_session(
        pairing_token=pairing.token,
        label="CEO Chrome",
        endpoint_kind="cdp",
        endpoint_url="http://127.0.0.1:9222",
    )

    assert session.active is True


def test_storage_state_session_writes_only_ignored_state_dir(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    pairing = service.create_pairing(label="OTP storage")

    session = service.register_session(
        pairing_token=pairing.token,
        label="OTP storage",
        endpoint_kind="storage_state",
        storage_state={"cookies": [], "origins": []},
    )
    config = service.e2e_config()

    assert session.storage_state_ref
    assert config["mode"] == "storage_state"
    assert config["storage_state_path"].startswith(str(tmp_path))


def test_e2e_config_can_pin_specific_session_without_changing_active(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    first_pairing = service.create_pairing(label="Work A")
    second_pairing = service.create_pairing(label="Work B")

    first = service.register_session(
        pairing_token=first_pairing.token,
        label="Work A",
        endpoint_kind="cdp",
        endpoint_url="http://127.0.0.1:9222",
    )
    second = service.register_session(
        pairing_token=second_pairing.token,
        label="Work B",
        endpoint_kind="cdp",
        endpoint_url="http://127.0.0.1:9223",
    )
    service.select_session(first.session_id)

    config = service.e2e_config(session_id=second.session_id)

    assert config["mode"] == "cdp"
    assert config["session_id"] == second.session_id
    assert config["cdp_url"] == "http://127.0.0.1:9223"
    assert service.active_session().session_id == first.session_id


def test_e2e_config_reports_missing_pinned_session(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )

    config = service.e2e_config(session_id="bb-missing")

    assert config["mode"] == "unavailable"
    assert config["session_id"] == "bb-missing"
    assert config["headless_fallback"] is False
    assert "browser bridge session not found: bb-missing" in config["error"]


def test_session_registry_persists_sessions_and_leases(tmp_path) -> None:
    first = SessionRegistry(state_dir=tmp_path)
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=first,
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="PC Agent Chrome A",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "9222",
            "endpoint_kind": "local_agent",
        },
    )
    leased = first.acquire_lease(owner="job-a", preferred_session_id=session.session_id, ttl_seconds=60)

    assert leased.session_id == session.session_id
    assert leased.lease_owner == "job-a"

    second = SessionRegistry(state_dir=tmp_path)
    loaded = second.get(session.session_id)

    assert loaded is not None
    assert loaded.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT
    assert loaded.endpoint.metadata["agent_id"] == "ceo-pc"
    assert loaded.lease_owner == "job-a"

    assert second.release_lease(owner="job-a", session_id=session.session_id) == 1
    assert second.get(session.session_id).lease_owner == ""


def test_session_registry_retire_marks_stale_and_skips_work_key_lookup(tmp_path) -> None:
    registry = SessionRegistry(state_dir=tmp_path)
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=registry,
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="NTV2 VVIC scrape",
        endpoint_kind="local_agent",
        metadata={"agent_id": "ceo-pc", "port": "9222", "endpoint_kind": "local_agent"},
        work_key="ntv2-vvic-scrape",
    )

    retired = registry.retire_session(
        session.session_id,
        stale_reason="STALE_TARGET",
        clear_work_key=True,
        clear_lease=True,
    )

    assert retired is not None
    assert registry.find_by_work_key("ntv2-vvic-scrape") is None
    loaded = registry.get(session.session_id)
    assert loaded is not None
    assert loaded.work_key == ""
    assert loaded.endpoint.metadata["stale"] is True
    assert loaded.endpoint.metadata["stale_reason"] == "STALE_TARGET"


def test_pairing_manager_persists_unconsumed_pairings(tmp_path) -> None:
    first = PairingManager(default_ttl_seconds=60, state_dir=tmp_path)
    pairing = first.create_pairing(label="CEO Chrome", created_by="test")

    second = PairingManager(default_ttl_seconds=60, state_dir=tmp_path)
    record = second.consume(pairing.token)

    assert record.pairing_id == pairing.pairing_id
    assert record.label == "CEO Chrome"

    third = PairingManager(default_ttl_seconds=60, state_dir=tmp_path)
    with pytest.raises(ValueError, match="pairing token invalid"):
        third.consume(pairing.token)


def test_active_api_route_urls_include_active_container(monkeypatch) -> None:
    monkeypatch.setenv("AADS_ACTIVE_CONTAINER", "aads-server-green")
    monkeypatch.setattr(
        BrowserBridgeService,
        "_docker_default_gateway_hosts",
        staticmethod(lambda: ["172.18.0.1"]),
    )

    urls = BrowserBridgeService._active_api_route_urls("8102")

    assert urls[:3] == [
        "http://aads-server-green:8080/api/v1/pc-agent/route-execute",
        "http://127.0.0.1:8080/api/v1/pc-agent/route-execute",
        "http://127.0.0.1:8102/api/v1/pc-agent/route-execute",
    ]
    assert "http://host.docker.internal:8102/api/v1/pc-agent/route-execute" in urls
    assert "http://172.18.0.1:8102/api/v1/pc-agent/route-execute" in urls
    assert "http://172.17.0.1:8102/api/v1/pc-agent/route-execute" in urls
    assert "http://aads-server-green:8080/api/v1/pc-agent/route-execute" in urls


def test_docker_default_gateway_hosts_reads_proc_route(monkeypatch, tmp_path) -> None:
    route_file = tmp_path / "route"
    route_file.write_text(
        "Iface\tDestination\tGateway \tFlags\n"
        "eth0\t00000000\t010012AC\t0003\n",
        encoding="utf-8",
    )
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):  # noqa: ANN001
        if path == "/proc/net/route":
            return real_open(route_file, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setenv("AADS_DOCKER_HOST_GATEWAY", "172.19.0.1")
    monkeypatch.setattr(service_module, "open", fake_open, raising=False)

    assert BrowserBridgeService._docker_default_gateway_hosts() == ["172.19.0.1", "172.18.0.1"]


def test_active_api_ports_include_blue_green_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("AADS_ACTIVE_PORT", "8100")

    ports = BrowserBridgeService._active_api_ports()

    assert ports == ["8100", "8102"]


@pytest.mark.asyncio
async def test_active_api_fallback_surfaces_non_routing_http_error(monkeypatch) -> None:
    service = BrowserBridgeService()
    calls: list[str] = []

    monkeypatch.setattr(
        BrowserBridgeService,
        "_active_api_ports",
        classmethod(lambda cls: ["8102", "8100"]),
    )
    monkeypatch.setattr(
        BrowserBridgeService,
        "_docker_default_gateway_hosts",
        staticmethod(lambda: []),
    )

    def fake_urlopen(req, timeout):  # noqa: ANN001, ARG001
        calls.append(req.full_url)
        if "8102" in req.full_url:
            body = {
                "detail": {
                    "status": "error",
                    "error_code": "PC_AGENT_OFFLINE",
                    "message": "agent '2e9379a1-fed' is offline",
                }
            }
        else:
            body = {
                "detail": {
                    "status": "error",
                    "error_code": None,
                    "message": "파일을 찾을 수 없습니다",
                    "result": {
                        "result": {
                            "error": "파일을 찾을 수 없습니다",
                            "missing": ["C:\\AADS_UPLOAD_PROBE_DO_NOT_EXIST.jpg"],
                        }
                    },
                }
            }
        raise urllib.error.HTTPError(
            req.full_url,
            503,
            "Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(json.dumps(body).encode("utf-8")),
        )

    monkeypatch.setattr(service_module.urllib.request, "urlopen", fake_urlopen)

    result = await service._execute_pc_agent_route_via_active_api(
        command_type="browser_file_upload",
        params={
            "port": 9222,
            "selector": "input[type=file]",
            "file_paths": ["C:\\AADS_UPLOAD_PROBE_DO_NOT_EXIST.jpg"],
        },
        agent_id="2e9379a1-fed",
        job_type="browser_bridge_probe",
        required_capabilities=["interactive_browser"],
    )

    assert len(calls) == 4
    assert calls[:3] == [
        "http://127.0.0.1:8102/api/v1/pc-agent/route-execute",
        "http://host.docker.internal:8102/api/v1/pc-agent/route-execute",
        "http://172.17.0.1:8102/api/v1/pc-agent/route-execute",
    ]
    assert result is not None
    assert result["message"] == "파일을 찾을 수 없습니다"
    assert result["result"]["result"]["missing"] == ["C:\\AADS_UPLOAD_PROBE_DO_NOT_EXIST.jpg"]


@pytest.mark.asyncio
async def test_ensure_pc_agent_cdp_registers_local_agent_session(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )

    captured_kwargs = {}

    async def fake_execute_routed_command(**kwargs):
        captured_kwargs.update(kwargs)
        assert kwargs["command_type"] == "browser_launch"
        assert kwargs["required_capabilities"] == ["interactive_browser"]
        return {
            "status": "success",
            "lease": {"agent_id": "ceo-pc"},
            "result": {
                "result": {
                    "port": 9333,
                    "user_data_dir": "C:/AADS/chrome/isolated-a",
                    "websocket_debugger_url": "ws://127.0.0.1:9333/devtools/browser/test",
                }
            },
        }

    from app.services import pc_agent_manager as manager_module

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    session = await service.ensure_pc_agent_cdp_session(
        label="Worker A",
        url="https://aads.newtalk.kr/",
        preferred_port=9333,
        work_key="ntv2-china-sourcing-admin",
    )

    assert session.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT
    assert session.endpoint.metadata["agent_id"] == "ceo-pc"
    assert session.endpoint.metadata["port"] == "9333"
    assert session.endpoint.metadata["cdp_url"] == "pc-agent://ceo-pc/cdp/9333"
    assert captured_kwargs["params"]["work_key"] == "ntv2-china-sourcing-admin"
    assert captured_kwargs["params"]["isolation_id"] == "ntv2-china-sourcing-admin"
    assert session.work_key == "ntv2-china-sourcing-admin"


@pytest.mark.asyncio
async def test_local_agent_context_does_not_require_server_playwright(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="CEO PC Chrome",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "9222",
            "endpoint_kind": "local_agent",
            "last_url": "about:blank",
        },
        activate=True,
    )

    async def fail_playwright():
        raise AssertionError("local_agent must not start Playwright on the server")

    monkeypatch.setattr(service, "_ensure_playwright", fail_playwright)

    context, error = await service.acquire_playwright_context(session_id=session.session_id)

    assert error is None
    assert context.pages[0].url == "about:blank"


@pytest.mark.asyncio
async def test_local_agent_commands_fallback_to_active_api(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="CEO PC Chrome",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "9222",
            "endpoint_kind": "local_agent",
            "last_url": "about:blank",
        },
        activate=True,
    )

    from app.services import pc_agent_manager as manager_module

    async def fake_local_execute(**_kwargs):
        return {"status": "error", "error_code": "PC_AGENT_OFFLINE", "message": "agent offline"}

    active_calls: list[dict] = []

    async def fake_active_execute(**kwargs):
        active_calls.append(kwargs)
        return {
            "status": "success",
            "lease": {"agent_id": "ceo-pc"},
            "result": {"result": {"ok": True}},
        }

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_local_execute)
    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", fake_active_execute)

    context, error = await service.acquire_playwright_context(session_id=session.session_id)

    assert error is None
    await context.pages[0].goto("https://aads.newtalk.kr/")
    assert active_calls
    assert active_calls[0]["command_type"] == "browser_navigate"
    assert active_calls[0]["agent_id"] == "ceo-pc"
    assert active_calls[0]["params"]["port"] == 9222
    assert active_calls[0]["queue_wait_timeout_seconds"] == 60
    assert active_calls[0]["command_timeout_seconds"] == 180
    assert active_calls[0]["lease_ttl_seconds"] == 210


@pytest.mark.asyncio
async def test_local_agent_page_tracks_redirect_and_invokes_function_expressions(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="CEO PC Chrome",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "9222",
            "endpoint_kind": "local_agent",
            "last_url": "about:blank",
        },
        activate=True,
    )

    from app.services import pc_agent_manager as manager_module

    expressions: list[str] = []

    async def fake_execute_routed_command(**kwargs):
        if kwargs["command_type"] == "browser_navigate":
            return {"status": "success", "result": {"result": {"ok": True}}}
        expression = kwargs["params"]["expression"]
        expressions.append(expression)
        value = "https://aads.newtalk.kr/login" if expression == "window.location.href" else "called"
        return {"status": "success", "result": {"result": {"value": value}}}

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    context, error = await service.acquire_playwright_context(session_id=session.session_id)

    assert error is None
    page = context.pages[0]
    await page.goto("https://aads.newtalk.kr/chat/session-1")
    assert page.url == "https://aads.newtalk.kr/login"
    assert session.endpoint.metadata["last_url"] == "https://aads.newtalk.kr/login"

    result = await page.evaluate("() => 'called'")
    assert result == "called"
    assert expressions[-1] == "(() => 'called')()"


@pytest.mark.asyncio
async def test_local_agent_browser_input_file_and_download_commands(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="CEO PC Chrome",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "9222",
            "endpoint_kind": "local_agent",
            "last_url": "about:blank",
        },
        activate=True,
    )

    from app.services import pc_agent_manager as manager_module

    async def fake_local_execute(**_kwargs):
        return {"status": "error", "error_code": "PC_AGENT_OFFLINE", "message": "agent offline"}

    active_calls: list[dict] = []

    async def fake_active_execute(**kwargs):
        active_calls.append(kwargs)
        command_type = kwargs["command_type"]
        data = {"ok": True}
        if command_type == "browser_download":
            data = {"path": "C:/Users/CEO/AADSDownloads/image.zip", "size": 1234}
        return {
            "status": "success",
            "lease": {"agent_id": "ceo-pc"},
            "result": {"result": data},
        }

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_local_execute)
    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", fake_active_execute)

    context, error = await service.acquire_playwright_context(session_id=session.session_id)

    assert error is None
    page = context.pages[0]
    await page.press_key("Enter", selector="input[name=q]")
    await page.select_option("select[name=category]", "outer")
    await page.set_checked("input[name=agree]", True)
    await page.set_input_files("input[type=file]", ["C:/Users/CEO/Pictures/a.jpg"])
    downloaded = await page.download("button.download", download_dir="C:/Users/CEO/AADSDownloads")

    assert downloaded["path"].endswith("image.zip")
    assert [call["command_type"] for call in active_calls] == [
        "browser_press_key",
        "browser_select_option",
        "browser_check",
        "browser_file_upload",
        "browser_download",
    ]
    assert active_calls[3]["params"]["file_paths"] == ["C:/Users/CEO/Pictures/a.jpg"]
    assert active_calls[4]["params"]["download_dir"] == "C:/Users/CEO/AADSDownloads"


@pytest.mark.asyncio
async def test_acquire_specific_session_does_not_change_active_session(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    first_pairing = service.create_pairing(label="Work A")
    second_pairing = service.create_pairing(label="Work B")

    first = service.register_session(
        pairing_token=first_pairing.token,
        label="Work A",
        endpoint_kind="cdp",
        endpoint_url="http://127.0.0.1:9222",
    )
    second = service.register_session(
        pairing_token=second_pairing.token,
        label="Work B",
        endpoint_kind="cdp",
        endpoint_url="http://127.0.0.1:9223",
    )
    service.select_session(first.session_id)

    async def fake_context(session):
        return {"session_id": session.session_id}

    service._context_for_session = fake_context  # type: ignore[method-assign]

    context, error = await service.acquire_playwright_context(session_id=second.session_id)

    assert error is None
    assert context == {"session_id": second.session_id}
    assert service.active_session().session_id == first.session_id


@pytest.mark.asyncio
async def test_acquire_specific_session_reports_missing_session(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )

    context, error = await service.acquire_playwright_context(session_id="bb-missing")

    assert context is None
    assert "browser bridge session not found: bb-missing" in error


@pytest.mark.asyncio
async def test_work_key_session_does_not_reuse_protected_sinsang_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "app.services.pc_agent_manager.pc_agent_manager.get_agent",
        lambda _agent_id: object(),
    )
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    sinsang = service.register_trusted_session(
        label="NTV2 Sinsang registration",
        endpoint_kind="local_agent",
        metadata={"agent_id": "ceo-pc", "port": "9222", "endpoint_kind": "local_agent"},
        work_key="ntv2-sinsang-registration",
        protected=True,
        activate=True,
    )
    created: list[dict] = []

    async def fake_ensure_pc_agent_cdp_session(**kwargs):
        created.append(kwargs)
        port = str(9300 + len(created))
        return service.register_trusted_session(
            label=kwargs["label"],
            endpoint_kind="local_agent",
            metadata={"agent_id": "ceo-pc", "port": port, "endpoint_kind": "local_agent"},
            work_key=kwargs["work_key"],
            protected=kwargs["protected"],
            activate=kwargs["activate"],
        )

    monkeypatch.setattr(service, "ensure_pc_agent_cdp_session", fake_ensure_pc_agent_cdp_session)

    china = await service.ensure_work_session(work_key="ntv2-china-sourcing-admin")
    again = await service.ensure_work_session(work_key="ntv2-china-sourcing-admin")

    assert china.session_id != sinsang.session_id
    assert again.session_id == china.session_id
    assert len(created) == 1
    assert created[0]["isolation_id"] == "ntv2-china-sourcing-admin"
    assert created[0]["activate"] is False
    assert service.active_session().session_id == sinsang.session_id

    status = service.work_session_status()
    work_keys = {item["work_key"]: item for item in status["work_sessions"]}
    assert work_keys["ntv2-sinsang-registration"]["protected"] is True
    assert work_keys["ntv2-china-sourcing-admin"]["session_id"] == china.session_id
    assert work_keys["ntv2-china-sourcing-admin"]["last_used_at"]


@pytest.mark.asyncio
async def test_work_key_session_recreates_stale_disconnected_context(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    stale = service.register_trusted_session(
        label="NTV2 China sourcing admin",
        endpoint_kind="cdp",
        endpoint_url="http://127.0.0.1:9333",
        work_key="ntv2-china-sourcing-admin",
        activate=True,
    )

    class DisconnectedBrowser:
        def is_connected(self) -> bool:
            return False

    service._session_browsers[stale.session_id] = DisconnectedBrowser()

    async def fake_ensure_pc_agent_cdp_session(**kwargs):
        return service.register_trusted_session(
            label=kwargs["label"],
            endpoint_kind="local_agent",
            metadata={"agent_id": "ceo-pc", "port": "9334", "endpoint_kind": "local_agent"},
            work_key=kwargs["work_key"],
            protected=kwargs["protected"],
            activate=kwargs["activate"],
        )

    monkeypatch.setattr(service, "ensure_pc_agent_cdp_session", fake_ensure_pc_agent_cdp_session)

    recreated = await service.ensure_work_session(work_key="ntv2-china-sourcing-admin")

    assert recreated.session_id != stale.session_id
    assert recreated.work_key == "ntv2-china-sourcing-admin"
    assert service.sessions.get(stale.session_id).work_key == ""
    assert service.active_session().session_id == stale.session_id
