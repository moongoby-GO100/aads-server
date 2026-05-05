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
