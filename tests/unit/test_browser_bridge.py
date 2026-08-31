from __future__ import annotations

import io
import json
import urllib.error
import builtins
from typing import Any

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


def test_session_registry_prunes_only_unleased_unprotected_stale_sessions(tmp_path) -> None:
    registry = SessionRegistry(state_dir=tmp_path)
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=registry,
        storage_states=StorageStateManager(tmp_path),
    )
    stale = service.register_trusted_session(
        label="stale",
        endpoint_kind="local_agent",
        metadata={"stale": True, "agent_id": "ceo-pc"},
    )
    leased = service.register_trusted_session(
        label="leased",
        endpoint_kind="local_agent",
        metadata={"stale": True, "agent_id": "ceo-pc"},
    )
    protected = service.register_trusted_session(
        label="protected",
        endpoint_kind="local_agent",
        metadata={"stale": True, "agent_id": "ceo-pc"},
        protected=True,
    )
    healthy = service.register_trusted_session(
        label="healthy",
        endpoint_kind="local_agent",
        metadata={"agent_id": "ceo-pc"},
        work_key="healthy-work",
    )

    registry.acquire_lease(owner="job-a", preferred_session_id=leased.session_id)

    assert registry.prune_stale_sessions() == 1
    assert registry.get(stale.session_id) is None
    assert registry.get(leased.session_id) is not None
    assert registry.get(protected.session_id) is not None
    assert registry.get(healthy.session_id) is not None


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
    monkeypatch.setattr(BrowserBridgeService, "_running_in_docker", staticmethod(lambda: True))
    monkeypatch.setattr(
        BrowserBridgeService,
        "_docker_default_gateway_hosts",
        staticmethod(lambda: ["172.18.0.1"]),
    )

    urls = BrowserBridgeService._active_api_route_urls("8102")

    assert urls[:5] == [
        "http://aads-server-green:8080/api/v1/pc-agent/route-execute",
        "http://aads-server:8080/api/v1/pc-agent/route-execute",
        "http://127.0.0.1:8080/api/v1/pc-agent/route-execute",
        "http://127.0.0.1:8102/api/v1/pc-agent/route-execute",
        "http://host.docker.internal:8102/api/v1/pc-agent/route-execute",
    ]
    assert "http://host.docker.internal:8102/api/v1/pc-agent/route-execute" in urls
    assert "http://172.18.0.1:8102/api/v1/pc-agent/route-execute" in urls
    assert "http://172.17.0.1:8102/api/v1/pc-agent/route-execute" in urls
    assert "http://aads-server-green:8080/api/v1/pc-agent/route-execute" in urls


def test_active_api_route_urls_named_container_before_loopback_8080_and_external_ports(monkeypatch) -> None:
    """Sidecar containers must try Docker service DNS before loopback.

    In yeoljeong-finance-worker, 127.0.0.1:8080 points back to the worker
    itself, not the AADS API container.
    """
    monkeypatch.setattr(
        BrowserBridgeService,
        "_active_container_name",
        staticmethod(lambda: ""),
    )
    monkeypatch.setattr(BrowserBridgeService, "_running_in_docker", staticmethod(lambda: True))
    monkeypatch.setattr(
        BrowserBridgeService,
        "_docker_default_gateway_hosts",
        staticmethod(lambda: []),
    )

    urls = BrowserBridgeService._active_api_route_urls("8100")

    loopback_idx = urls.index("http://127.0.0.1:8080/api/v1/pc-agent/route-execute")
    named_idx = urls.index("http://aads-server:8080/api/v1/pc-agent/route-execute")
    external_idx = urls.index("http://127.0.0.1:8100/api/v1/pc-agent/route-execute")

    assert urls[0] == "http://aads-server:8080/api/v1/pc-agent/route-execute"
    assert named_idx < loopback_idx
    assert loopback_idx < external_idx


def test_active_api_route_urls_use_loopback_only_on_host(monkeypatch) -> None:
    monkeypatch.setattr(BrowserBridgeService, "_running_in_docker", staticmethod(lambda: False))
    monkeypatch.setattr(
        BrowserBridgeService,
        "_docker_default_gateway_hosts",
        staticmethod(lambda: ["172.18.0.1"]),
    )

    urls = BrowserBridgeService._active_api_route_urls("8102")

    assert urls == [
        "http://127.0.0.1:8102/api/v1/pc-agent/route-execute",
        "http://127.0.0.1:8080/api/v1/pc-agent/route-execute",
    ]


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


def test_pc_agent_timeout_wrapper_with_embedded_success_is_accepted() -> None:
    result = BrowserBridgeService._coerce_pc_agent_embedded_success(
        {
            "status": "error",
            "error_code": "COMMAND_TIMEOUT",
            "message": "command timeout",
            "result": {
                "status": "success",
                "result": {"value": {"url": "https://store.coupangeats.com/merchant/login"}},
            },
        }
    )

    assert result["status"] == "success"
    assert result["error_code"] == ""
    assert result["late_success_from_error_code"] == "COMMAND_TIMEOUT"
    assert result["result"]["result"]["value"]["url"].endswith("/merchant/login")


@pytest.mark.asyncio
async def test_active_api_fallback_surfaces_non_routing_http_error(monkeypatch) -> None:
    service = BrowserBridgeService()
    calls: list[str] = []

    monkeypatch.setattr(
        BrowserBridgeService,
        "_active_api_ports",
        classmethod(lambda cls: ["8102"]),
    )
    monkeypatch.setattr(
        BrowserBridgeService,
        "_docker_default_gateway_hosts",
        staticmethod(lambda: []),
    )
    monkeypatch.setattr(
        BrowserBridgeService,
        "_active_container_name",
        staticmethod(lambda: ""),
    )

    def fake_urlopen(req, timeout):  # noqa: ANN001, ARG001
        calls.append(req.full_url)
        if req.full_url in {
            "http://127.0.0.1:8080/api/v1/pc-agent/route-execute",
            "http://aads-server-green:8080/api/v1/pc-agent/route-execute",
        }:
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

    assert calls == [
        "http://aads-server-green:8080/api/v1/pc-agent/route-execute",
        "http://aads-server:8080/api/v1/pc-agent/route-execute",
    ]
    assert result is not None
    assert result["message"] == "파일을 찾을 수 없습니다"
    assert result["result"]["result"]["missing"] == ["C:\\AADS_UPLOAD_PROBE_DO_NOT_EXIST.jpg"]


@pytest.mark.asyncio
async def test_active_api_fallback_does_not_retry_online_agent_when_pinned_default_offline(monkeypatch) -> None:
    service = BrowserBridgeService()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        BrowserBridgeService,
        "_active_api_ports",
        classmethod(lambda cls: ["8102"]),
    )
    monkeypatch.setattr(
        BrowserBridgeService,
        "_docker_default_gateway_hosts",
        staticmethod(lambda: []),
    )
    monkeypatch.setattr(
        BrowserBridgeService,
        "_active_container_name",
        staticmethod(lambda: ""),
    )

    class FakeResponse:
        def __init__(self, body: dict[str, Any]):
            self._body = json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout):  # noqa: ANN001, ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/status"):
            calls.append((url, "GET"))
            return FakeResponse(
                {
                    "agents": [
                        {
                            "agent_id": "online-browser-agent",
                            "status": "online",
                            "heartbeat_age_seconds": 1.0,
                            "capabilities": ["interactive_browser", "chrome_cdp"],
                        }
                    ]
                }
            )

        body = json.loads(req.data.decode("utf-8"))
        calls.append((url, str(body.get("agent_id") or "")))
        if body.get("agent_id") == "online-browser-agent":
            return FakeResponse({"status": "success", "lease": {"agent_id": "online-browser-agent"}})

        raise urllib.error.HTTPError(
            url,
            503,
            "Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(
                json.dumps(
                    {
                        "detail": {
                            "status": "error",
                            "error_code": "PC_AGENT_OFFLINE",
                            "message": "default browser PC agent 'offline-default' is offline",
                        }
                    }
                ).encode("utf-8")
            ),
        )

    monkeypatch.setattr(service_module.urllib.request, "urlopen", fake_urlopen)

    result = await service._execute_pc_agent_route_via_active_api(
        command_type="browser_launch",
        params={"url": "about:blank"},
        agent_id="",
        job_type="browser_bridge_launch",
        required_capabilities=["interactive_browser"],
    )

    assert result is None
    assert calls
    assert all(method_or_agent == "" for _url, method_or_agent in calls)
    assert all(not url.endswith("/status") for url, _method_or_agent in calls)


@pytest.mark.asyncio
async def test_active_api_fallback_retries_online_agent_for_generic_offline_route(monkeypatch) -> None:
    service = BrowserBridgeService()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        BrowserBridgeService,
        "_active_api_ports",
        classmethod(lambda cls: ["8102"]),
    )
    monkeypatch.setattr(
        BrowserBridgeService,
        "_docker_default_gateway_hosts",
        staticmethod(lambda: []),
    )
    monkeypatch.setattr(
        BrowserBridgeService,
        "_active_container_name",
        staticmethod(lambda: ""),
    )

    class FakeResponse:
        def __init__(self, body: dict[str, Any]):
            self._body = json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout):  # noqa: ANN001, ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/status"):
            calls.append((url, "GET"))
            return FakeResponse(
                {
                    "agents": [
                        {
                            "agent_id": "online-browser-agent",
                            "status": "online",
                            "heartbeat_age_seconds": 1.0,
                            "capabilities": ["interactive_browser", "chrome_cdp"],
                        }
                    ]
                }
            )

        body = json.loads(req.data.decode("utf-8"))
        calls.append((url, str(body.get("agent_id") or "")))
        if body.get("agent_id") == "online-browser-agent":
            return FakeResponse({"status": "success", "lease": {"agent_id": "online-browser-agent"}})

        raise urllib.error.HTTPError(
            url,
            503,
            "Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(
                json.dumps(
                    {
                        "detail": {
                            "status": "error",
                            "error_code": "PC_AGENT_OFFLINE",
                            "message": "no online PC agent",
                        }
                    }
                ).encode("utf-8")
            ),
        )

    monkeypatch.setattr(service_module.urllib.request, "urlopen", fake_urlopen)

    result = await service._execute_pc_agent_route_via_active_api(
        command_type="browser_launch",
        params={"url": "about:blank"},
        agent_id="",
        job_type="browser_bridge_launch",
        required_capabilities=["interactive_browser"],
    )

    assert result == {"status": "success", "lease": {"agent_id": "online-browser-agent"}}
    assert calls[:3] == [
        ("http://aads-server-green:8080/api/v1/pc-agent/route-execute", ""),
        ("http://aads-server-green:8080/api/v1/pc-agent/status", "GET"),
        ("http://aads-server-green:8080/api/v1/pc-agent/route-execute", "online-browser-agent"),
    ]


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
        command_timeout_seconds=45,
    )

    assert session.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT
    assert session.endpoint.metadata["agent_id"] == "ceo-pc"
    assert session.endpoint.metadata["port"] == "9333"
    assert session.endpoint.metadata["cdp_url"] == "pc-agent://ceo-pc/cdp/9333"
    assert captured_kwargs["params"]["work_key"] == "ntv2-china-sourcing-admin"
    assert captured_kwargs["params"]["isolation_id"] == "ntv2-china-sourcing-admin"
    assert captured_kwargs["params"]["new_window"] is False
    assert captured_kwargs["params"]["ready_timeout_seconds"] == 40.0
    assert session.work_key == "ntv2-china-sourcing-admin"


@pytest.mark.asyncio
async def test_ensure_pc_agent_cdp_falls_back_to_active_api_when_no_local_agent(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )

    from app.services import pc_agent_manager as manager_module

    async def fake_local_execute(**_kwargs):
        return {"status": "error", "error_code": "NO_CAPABLE_AGENT", "message": "no capable local agent"}

    active_calls: list[dict] = []

    async def fake_active_execute(**kwargs):
        active_calls.append(kwargs)
        return {
            "status": "success",
            "lease": {"agent_id": "ceo-pc"},
            "result": {
                "result": {
                    "port": 9444,
                    "user_data_dir": "C:/AADS/chrome/yeoljeong",
                    "websocket_debugger_url": "ws://127.0.0.1:9444/devtools/browser/test",
                }
            },
        }

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_local_execute)
    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", fake_active_execute)

    session = await service.ensure_pc_agent_cdp_session(
        label="Yeoljeong Baemin",
        url="https://self.baemin.com/",
        work_key="yeoljeong-delivery-baemin-biz-junghwa-test",
    )

    assert active_calls
    assert active_calls[0]["command_type"] == "browser_launch"
    assert active_calls[0]["params"]["work_key"] == "yeoljeong-delivery-baemin-biz-junghwa-test"
    assert active_calls[0]["params"]["new_window"] is False
    assert session.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT
    assert session.endpoint.metadata["agent_id"] == "ceo-pc"
    assert session.endpoint.metadata["port"] == "9444"


@pytest.mark.asyncio
async def test_ensure_pc_agent_cdp_sidecar_routes_active_api_first(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )

    from app.services import pc_agent_manager as manager_module

    async def fail_local_execute(**_kwargs):
        raise AssertionError("sidecar worker should not wait on the local PC Agent manager")

    active_calls: list[dict] = []

    async def fake_active_execute(**kwargs):
        active_calls.append(kwargs)
        return {
            "status": "success",
            "lease": {"agent_id": "oby-ceo"},
            "result": {
                "result": {
                    "port": 9666,
                    "user_data_dir": "C:/AADS/chrome/yeoljeong",
                    "websocket_debugger_url": "ws://127.0.0.1:9666/devtools/browser/test",
                }
            },
        }

    monkeypatch.setenv("AADS_SERVICE_ROLE", "yeoljeong-finance-worker")
    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fail_local_execute)
    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", fake_active_execute)

    session = await service.ensure_pc_agent_cdp_session(
        label="Yeoljeong Coupang",
        url="https://store.coupangeats.com/",
        work_key="yeoljeong-delivery-coupangeats-biz-mia-test",
    )

    assert active_calls
    assert active_calls[0]["command_type"] == "browser_launch"
    assert active_calls[0]["params"]["new_window"] is False
    assert active_calls[0]["queue_wait_timeout_seconds"] == service_module.SIDECAR_QUEUE_WAIT_SECONDS
    assert active_calls[0]["command_timeout_seconds"] == service_module.SIDECAR_LAUNCH_TIMEOUT_SECONDS
    assert session.endpoint.metadata["agent_id"] == "oby-ceo"
    assert session.endpoint.metadata["port"] == "9666"


@pytest.mark.asyncio
async def test_ensure_pc_agent_cdp_force_recreate_keeps_profile_by_default(monkeypatch, tmp_path) -> None:
    """force_recreate 시에도 기본값은 Chrome 프로필(isolation_id) 유지 — 로그인 쿠키 보존."""
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )

    from app.services import pc_agent_manager as manager_module

    captured_kwargs = {}

    async def fake_execute_routed_command(**kwargs):
        captured_kwargs.update(kwargs)
        return {
            "status": "success",
            "lease": {"agent_id": "ceo-pc"},
            "result": {
                "result": {
                    "port": 9555,
                    "user_data_dir": "C:/AADS/chrome/yeoljeong",
                    "websocket_debugger_url": "ws://127.0.0.1:9555/devtools/browser/test",
                }
            },
        }

    monkeypatch.delenv("AADS_BROWSER_PROFILE_STABLE_ON_RECREATE", raising=False)
    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    session = await service.ensure_pc_agent_cdp_session(
        label="Yeoljeong Baemin",
        url="https://self.baemin.com/",
        work_key="yeoljeong-delivery-baemin-biz-junghwa-test",
        force_recreate=True,
    )

    assert session.work_key == "yeoljeong-delivery-baemin-biz-junghwa-test"
    assert captured_kwargs["params"]["work_key"] == "yeoljeong-delivery-baemin-biz-junghwa-test"
    assert captured_kwargs["params"]["isolation_id"] == "yeoljeong-delivery-baemin-biz-junghwa-test"


@pytest.mark.asyncio
async def test_ensure_pc_agent_cdp_force_recreate_closes_existing_work_key_first(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )

    from app.services import pc_agent_manager as manager_module

    calls: list[dict] = []

    async def fake_execute_routed_command(**kwargs):
        calls.append(kwargs)
        if kwargs["command_type"] == "browser_close_session":
            return {"status": "success", "result": {"result": {"session_released": True}}}
        return {
            "status": "success",
            "lease": {"agent_id": "collector-pc"},
            "result": {
                "result": {
                    "port": 9666,
                    "user_data_dir": "C:/AADS/chrome/yeoljeong",
                    "websocket_debugger_url": "ws://127.0.0.1:9666/devtools/browser/test",
                }
            },
        }

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    session = await service.ensure_pc_agent_cdp_session(
        label="Yeoljeong Coupang",
        url="https://store.coupangeats.com/",
        work_key="yeoljeong-delivery-coupangeats-biz-junghwa-test",
        force_recreate=True,
    )

    assert [call["command_type"] for call in calls] == ["browser_close_session", "browser_launch"]
    assert calls[0]["params"]["work_key"] == "yeoljeong-delivery-coupangeats-biz-junghwa-test"
    assert calls[0]["params"]["close_browser"] is True
    assert calls[1]["params"]["url"] == "https://store.coupangeats.com/"
    assert session.endpoint.metadata["agent_id"] == "collector-pc"


@pytest.mark.asyncio
async def test_close_work_session_releases_non_protected_pc_agent_session(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="GO100 E2E command center",
        endpoint_kind="local_agent",
        metadata={"agent_id": "ceo-pc", "port": "9777", "endpoint_kind": "local_agent"},
        work_key="go100-e2e-command-center",
        protected=False,
        activate=False,
    )

    from app.services import pc_agent_manager as manager_module

    calls: list[dict] = []

    async def fake_execute_routed_command(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "result": {"result": {"session_released": True}}}

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    result = await service.close_work_session(
        "go100-e2e-command-center",
        reason="capture_screenshot_complete",
        close_tabs=True,
    )
    retired = service.sessions.get(session.session_id)

    assert result["status"] == "success"
    assert calls[0]["command_type"] == "browser_close_session"
    assert calls[0]["params"]["work_key"] == "go100-e2e-command-center"
    assert calls[0]["params"]["close_tabs"] is True
    assert calls[0]["params"]["close_browser"] is False
    assert retired is not None
    assert retired.work_key == ""
    assert retired.endpoint.metadata["stale"] is True
    assert retired.endpoint.metadata["stale_reason"] == "capture_screenshot_complete"


@pytest.mark.asyncio
async def test_close_work_session_skips_protected_session(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="NTV2 Sinsang registration",
        endpoint_kind="local_agent",
        metadata={"agent_id": "ceo-pc", "port": "9778", "endpoint_kind": "local_agent"},
        work_key="ntv2-sinsang-registration",
        protected=True,
        activate=False,
    )

    from app.services import pc_agent_manager as manager_module

    async def fail_execute_routed_command(**_kwargs):
        raise AssertionError("protected work session must not be closed")

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fail_execute_routed_command)

    result = await service.close_work_session("ntv2-sinsang-registration")
    kept = service.sessions.get(session.session_id)

    assert result["status"] == "skipped"
    assert result["reason"] == "protected_work_session"
    assert kept is not None
    assert kept.work_key == "ntv2-sinsang-registration"
    assert kept.endpoint.metadata.get("stale") is not True


@pytest.mark.asyncio
async def test_ensure_pc_agent_cdp_force_recreate_keeps_profile_by_default(monkeypatch, tmp_path) -> None:
    """force_recreate 시에도 기본값은 Chrome 프로필(isolation_id) 유지 — 로그인 쿠키 보존."""
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )

    from app.services import pc_agent_manager as manager_module

    captured_kwargs = {}

    async def fake_execute_routed_command(**kwargs):
        captured_kwargs.update(kwargs)
        return {
            "status": "success",
            "lease": {"agent_id": "ceo-pc"},
            "result": {
                "result": {
                    "port": 9555,
                    "user_data_dir": "C:/AADS/chrome/yeoljeong",
                    "websocket_debugger_url": "ws://127.0.0.1:9555/devtools/browser/test",
                }
            },
        }

    monkeypatch.delenv("AADS_BROWSER_PROFILE_STABLE_ON_RECREATE", raising=False)
    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    session = await service.ensure_pc_agent_cdp_session(
        label="Yeoljeong Baemin",
        url="https://self.baemin.com/",
        work_key="yeoljeong-delivery-baemin-biz-junghwa-test",
        force_recreate=True,
    )

    assert session.work_key == "yeoljeong-delivery-baemin-biz-junghwa-test"
    assert captured_kwargs["params"]["work_key"] == "yeoljeong-delivery-baemin-biz-junghwa-test"
    assert captured_kwargs["params"]["isolation_id"] == "yeoljeong-delivery-baemin-biz-junghwa-test"


@pytest.mark.asyncio
async def test_ensure_pc_agent_cdp_force_recreate_keeps_stable_isolation_profile(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )

    from app.services import pc_agent_manager as manager_module

    captured_kwargs = {}

    async def fake_execute_routed_command(**kwargs):
        captured_kwargs.update(kwargs)
        return {
            "status": "success",
            "lease": {"agent_id": "ceo-pc"},
            "result": {
                "result": {
                    "port": 9555,
                    "user_data_dir": "C:/AADS/chrome/yeoljeong-fresh",
                    "websocket_debugger_url": "ws://127.0.0.1:9555/devtools/browser/test",
                }
            },
        }

    monkeypatch.setenv("AADS_BROWSER_PROFILE_STABLE_ON_RECREATE", "0")
    monkeypatch.setenv("AADS_BROWSER_PROFILE_STABLE_ON_RECREATE", "0")
    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    session = await service.ensure_pc_agent_cdp_session(
        label="Yeoljeong Baemin",
        url="https://self.baemin.com/",
        work_key="yeoljeong-delivery-baemin-biz-junghwa-test",
        force_recreate=True,
    )

    assert session.work_key == "yeoljeong-delivery-baemin-biz-junghwa-test"
    assert captured_kwargs["params"]["work_key"] == "yeoljeong-delivery-baemin-biz-junghwa-test"
    assert captured_kwargs["params"]["isolation_id"] == "yeoljeong-delivery-baemin-biz-junghwa-test"


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
async def test_local_agent_commands_sidecar_route_active_api_first(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(state_dir=tmp_path),
        storage_states=StorageStateManager(tmp_path),
    )
    session = service.register_trusted_session(
        label="CEO PC Chrome",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "oby-ceo",
            "port": "9222",
            "endpoint_kind": "local_agent",
            "last_url": "about:blank",
        },
        activate=True,
    )

    from app.services import pc_agent_manager as manager_module

    async def fail_local_execute(**_kwargs):
        raise AssertionError("sidecar worker should not wait on the local PC Agent manager")

    active_calls: list[dict] = []

    async def fake_active_execute(**kwargs):
        active_calls.append(kwargs)
        return {
            "status": "success",
            "lease": {"agent_id": "oby-ceo"},
            "result": {"result": {"ok": True}},
        }

    monkeypatch.setenv("AADS_SERVICE_ROLE", "yeoljeong-finance-worker")
    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fail_local_execute)
    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", fake_active_execute)

    context, error = await service.acquire_playwright_context(session_id=session.session_id)

    assert error is None
    await context.pages[0].goto("https://store.coupangeats.com/")
    assert active_calls[0]["command_type"] == "browser_navigate"
    assert active_calls[0]["agent_id"] == "oby-ceo"
    assert active_calls[0]["params"].get("work_key", "") == ""
    assert active_calls[0]["queue_wait_timeout_seconds"] == service_module.SIDECAR_QUEUE_WAIT_SECONDS
    assert active_calls[0]["command_timeout_seconds"] == service_module.SIDECAR_NAVIGATION_TIMEOUT_SECONDS


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

    eval_params: list[dict[str, object]] = []

    async def fake_execute_routed_command(**kwargs):
        if kwargs["command_type"] == "browser_navigate":
            return {"status": "success", "result": {"result": {"ok": True}}}
        params = dict(kwargs["params"])
        eval_params.append(params)
        expression = params["expression"]
        value = "https://aads.newtalk.kr/login" if expression == "window.location.href" else "called"
        return {"status": "success", "result": {"result": {"value": value}}}

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    context, error = await service.acquire_playwright_context(session_id=session.session_id)

    assert error is None
    page = context.pages[0]
    await page.goto("https://aads.newtalk.kr/chat/session-1")
    assert page.url == "https://aads.newtalk.kr/login"
    assert session.endpoint.metadata["last_url"] == "https://aads.newtalk.kr/login"

    result = await page.evaluate("() => 'called'", timeout=30000)
    assert result == "called"
    assert eval_params[-1]["expression"] == "(() => 'called')()"
    assert eval_params[-1]["timeout_ms"] == 30000


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


def test_work_key_rebind_clears_metadata_and_latest_session_wins(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    work_key = "yeoljeong-bank-shinhan-individual-test"
    old = service.register_trusted_session(
        label="Old Shinhan browser",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "9222",
            "endpoint_kind": "local_agent",
            "last_url": "about:blank",
        },
        work_key=work_key,
        protected=False,
        activate=False,
    )

    newer = service.register_trusted_session(
        label="New Shinhan browser",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "32888",
            "endpoint_kind": "local_agent",
            "last_url": "https://bank.shinhan.com/rib/easy/index.jsp",
        },
        work_key=work_key,
        protected=False,
        activate=False,
    )

    released = service.sessions.get(old.session_id)
    found = service.sessions.find_by_work_key(work_key)

    assert released is not None
    assert released.work_key == ""
    assert "work_key" not in dict(released.endpoint.metadata or {})
    assert "protected" not in dict(released.endpoint.metadata or {})
    assert found is not None
    assert found.session_id == newer.session_id


@pytest.mark.asyncio
async def test_work_key_session_recreates_about_blank_when_url_requested(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    work_key = "yeoljeong-bank-shinhan-individual-test"
    stale = service.register_trusted_session(
        label="Shinhan blank browser",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "9222",
            "endpoint_kind": "local_agent",
            "last_url": "about:blank",
        },
        work_key=work_key,
        activate=False,
    )
    created: list[dict] = []

    async def fake_ensure_pc_agent_cdp_session(**kwargs):
        created.append(kwargs)
        return service.register_trusted_session(
            label=kwargs["label"],
            endpoint_kind="local_agent",
            metadata={
                "agent_id": "ceo-pc",
                "port": "32888",
                "endpoint_kind": "local_agent",
                "last_url": kwargs["url"],
            },
            work_key=kwargs["work_key"],
            protected=kwargs["protected"],
            activate=kwargs["activate"],
        )

    monkeypatch.setattr(service, "ensure_pc_agent_cdp_session", fake_ensure_pc_agent_cdp_session)

    recreated = await service.ensure_work_session(
        work_key=work_key,
        url="https://bank.shinhan.com/rib/easy/index.jsp",
        preferred_port=32888,
    )
    retired = service.sessions.get(stale.session_id)

    assert recreated.session_id != stale.session_id
    assert recreated.work_key == work_key
    assert created[0]["preferred_port"] == 32888
    assert retired is not None
    assert retired.work_key == ""
    assert retired.endpoint.metadata["stale"] is True
    assert "work_key" not in dict(retired.endpoint.metadata or {})


@pytest.mark.asyncio
async def test_work_key_session_recreates_other_host_when_url_requested(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    work_key = "yeoljeong-bank-shinhan-individual-test"
    wrong_host = service.register_trusted_session(
        label="KIS browser",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "ceo-pc",
            "port": "9222",
            "endpoint_kind": "local_agent",
            "last_url": "https://kis.newtalk.kr/",
        },
        work_key=work_key,
        activate=False,
    )

    async def fake_ensure_pc_agent_cdp_session(**kwargs):
        return service.register_trusted_session(
            label=kwargs["label"],
            endpoint_kind="local_agent",
            metadata={
                "agent_id": "ceo-pc",
                "port": "32888",
                "endpoint_kind": "local_agent",
                "last_url": kwargs["url"],
            },
            work_key=kwargs["work_key"],
            protected=kwargs["protected"],
            activate=kwargs["activate"],
        )

    monkeypatch.setattr(service, "ensure_pc_agent_cdp_session", fake_ensure_pc_agent_cdp_session)

    recreated = await service.ensure_work_session(
        work_key=work_key,
        url="https://bank.shinhan.com/rib/easy/index.jsp",
    )

    assert recreated.session_id != wrong_host.session_id
    assert service.sessions.get(wrong_host.session_id).work_key == ""


@pytest.mark.asyncio
async def test_pc_agent_cdp_session_falls_back_to_browser_health_on_cdp_not_ready(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    calls: list[str] = []

    monkeypatch.setattr(service, "_route_pc_agent_via_active_api_first", lambda: True)

    async def fake_route(**kwargs):
        command_type = kwargs["command_type"]
        calls.append(command_type)
        if command_type == "browser_launch":
            return {
                "status": "error",
                "error_code": "CDP_NOT_READY",
                "message": "CDP endpoint 준비 실패",
            }
        if command_type == "browser_health":
            return {
                "status": "success",
                "lease": {"agent_id": "ceo-pc"},
                "result": {
                    "result": {
                        "port": 9222,
                        "work_key": "yeoljeong-bank-shinhan-individual-test",
                        "cdp_version": "Chrome/151",
                    }
                },
            }
        raise AssertionError(command_type)

    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", fake_route)

    session = await service.ensure_pc_agent_cdp_session(
        agent_id="ceo-pc",
        label="신한 간편조회",
        url="https://bank.shinhan.com/rib/easy/index.jsp",
        work_key="yeoljeong-bank-shinhan-individual-test",
        command_timeout_seconds=90,
    )

    assert calls == ["browser_launch", "browser_health"]
    assert session.work_key == "yeoljeong-bank-shinhan-individual-test"
    assert session.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT
    assert session.endpoint.metadata["agent_id"] == "ceo-pc"
    assert session.endpoint.metadata["port"] == "9222"


@pytest.mark.asyncio
async def test_pc_agent_cdp_session_falls_back_to_browser_tabs_when_health_fails(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    calls: list[str] = []

    monkeypatch.setattr(service, "_route_pc_agent_via_active_api_first", lambda: True)

    async def fake_route(**kwargs):
        command_type = kwargs["command_type"]
        calls.append(command_type)
        if command_type == "browser_launch":
            return {
                "status": "error",
                "error_code": "CDP_NOT_READY",
                "message": "CDP endpoint 준비 실패",
            }
        if command_type == "browser_health":
            return {
                "status": "error",
                "error_code": "CDP_NOT_READY",
                "message": "/json/version 응답 없음",
            }
        if command_type == "browser_tabs":
            return {
                "status": "success",
                "lease": {"agent_id": "ceo-pc"},
                "result": {
                    "result": {
                        "tabs": [
                            {
                                "id": "tab-1",
                                "title": "간편조회서비스",
                                "url": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
                                "type": "page",
                            }
                        ],
                        "count": 1,
                    }
                },
            }
        raise AssertionError(command_type)

    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", fake_route)

    session = await service.ensure_pc_agent_cdp_session(
        agent_id="ceo-pc",
        label="신한 간편조회",
        url="https://bank.shinhan.com/rib/easy/index.jsp",
        work_key="yeoljeong-bank-shinhan-individual-test",
        preferred_port=9222,
        command_timeout_seconds=90,
    )

    assert calls == ["browser_launch", "browser_health", "browser_tabs"]
    assert session.work_key == "yeoljeong-bank-shinhan-individual-test"
    assert session.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT
    assert session.endpoint.metadata["agent_id"] == "ceo-pc"
    assert session.endpoint.metadata["port"] == "9222"


@pytest.mark.asyncio
async def test_pc_agent_cdp_session_navigates_when_tabs_are_wrong_portal(monkeypatch, tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )
    calls: list[str] = []

    monkeypatch.setattr(service, "_route_pc_agent_via_active_api_first", lambda: True)

    async def fake_route(**kwargs):
        command_type = kwargs["command_type"]
        calls.append(command_type)
        if command_type == "browser_launch":
            return {
                "status": "error",
                "error_code": "CDP_NOT_READY",
                "message": "CDP endpoint 준비 실패",
            }
        if command_type == "browser_health":
            return {
                "status": "error",
                "error_code": "CDP_NOT_READY",
                "message": "/json/version 응답 없음",
            }
        if command_type == "browser_tabs":
            return {
                "status": "success",
                "lease": {"agent_id": "icu55hk"},
                "result": {
                    "result": {
                        "tabs": [
                            {
                                "id": "tab-1",
                                "title": "Dashboard",
                                "url": "https://aads.example.local/",
                                "type": "page",
                            }
                        ],
                        "count": 1,
                        "port": 9222,
                    }
                },
            }
        if command_type == "browser_navigate":
            assert kwargs["params"]["url"] == "https://bank.shinhan.com/rib/easy/index.jsp"
            assert kwargs["params"]["work_key"] == "yeoljeong-bank-shinhan-individual-test"
            return {
                "status": "success",
                "lease": {"agent_id": "icu55hk"},
                "result": {"result": {"ok": True}},
            }
        raise AssertionError(command_type)

    monkeypatch.setattr(service, "_execute_pc_agent_route_via_active_api", fake_route)

    session = await service.ensure_pc_agent_cdp_session(
        agent_id="icu55hk",
        label="신한 간편조회",
        url="https://bank.shinhan.com/rib/easy/index.jsp",
        work_key="yeoljeong-bank-shinhan-individual-test",
        preferred_port=9222,
        command_timeout_seconds=90,
    )

    assert calls == ["browser_launch", "browser_health", "browser_tabs", "browser_navigate"]
    assert session.work_key == "yeoljeong-bank-shinhan-individual-test"
    assert session.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT
    assert session.endpoint.metadata["agent_id"] == "icu55hk"
    assert session.endpoint.metadata["port"] == "9222"
    assert session.endpoint.metadata["last_url"] == "https://bank.shinhan.com/rib/easy/index.jsp"
