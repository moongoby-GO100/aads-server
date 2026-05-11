from __future__ import annotations

import pytest

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
        sessions=SessionRegistry(),
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
        sessions=SessionRegistry(),
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
        sessions=SessionRegistry(),
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
        sessions=SessionRegistry(),
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
        sessions=SessionRegistry(),
        storage_states=StorageStateManager(tmp_path),
    )

    config = service.e2e_config(session_id="bb-missing")

    assert config["mode"] == "unavailable"
    assert config["session_id"] == "bb-missing"
    assert config["headless_fallback"] is False
    assert "browser bridge session not found: bb-missing" in config["error"]


@pytest.mark.asyncio
async def test_acquire_specific_session_does_not_change_active_session(tmp_path) -> None:
    service = BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(),
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
        sessions=SessionRegistry(),
        storage_states=StorageStateManager(tmp_path),
    )

    context, error = await service.acquire_playwright_context(session_id="bb-missing")

    assert context is None
    assert "browser bridge session not found: bb-missing" in error
