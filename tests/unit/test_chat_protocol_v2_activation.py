"""WP06 activation gate: the browser may only switch adapters when every
operational gate passes and the server actually advertises chat.protocol.v2.
"""

from __future__ import annotations

import pytest

from app.models.chat import ChatProtocolCapabilitiesOut, StreamingStatusOut
from app.services import chat_protocol

_GATE_ENVS = (
    "AADS_CHAT_PROTOCOL_V2_ENABLED",
    "AADS_CHAT_V2_BROWSER_CONTRACT_VERIFIED",
    "AADS_CHAT_V2_READ_MODEL_ENABLED",
    "AADS_CHAT_WP04_MIGRATION_READY",
    "AADS_CHAT_WP04_CROSS_VERSION_READY",
    "AADS_CHAT_WP05_MIGRATION_READY",
    "AADS_CHAT_CURSOR_HMAC_SECRET",
)

_SECRET = "x" * 48


@pytest.fixture
def closed_env(monkeypatch):
    for name in _GATE_ENVS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _open_every_gate(monkeypatch):
    monkeypatch.setenv("AADS_CHAT_PROTOCOL_V2_ENABLED", "true")
    monkeypatch.setenv("AADS_CHAT_V2_BROWSER_CONTRACT_VERIFIED", "true")
    monkeypatch.setenv("AADS_CHAT_V2_READ_MODEL_ENABLED", "true")
    monkeypatch.setenv("AADS_CHAT_WP04_MIGRATION_READY", "true")
    monkeypatch.setenv("AADS_CHAT_WP04_CROSS_VERSION_READY", "true")
    monkeypatch.setenv("AADS_CHAT_WP05_MIGRATION_READY", "true")
    monkeypatch.setenv("AADS_CHAT_CURSOR_HMAC_SECRET", _SECRET)


def test_closed_environment_keeps_v2_unadvertised(closed_env):
    capabilities = chat_protocol.chat_protocol_capabilities()

    assert chat_protocol.chat_protocol_v2_activation_ready() is False
    assert capabilities["production_ready"] is False
    assert chat_protocol.CHAT_PROTOCOL_V2_CAPABILITY not in capabilities["capabilities"]
    assert capabilities["command_lifecycle"]["production_ready"] is False
    # Every documented requirement is still reported as pending.
    assert capabilities["pending_activation_requires"] == capabilities["activation_requires"]
    assert set(capabilities["activation_gates"]) == set(capabilities["activation_requires"])
    assert chat_protocol.negotiate_contract_version(None) == chat_protocol.CHAT_CONTRACT_V1


def test_partial_gates_do_not_advertise_v2(closed_env):
    closed_env.setenv("AADS_CHAT_WP05_MIGRATION_READY", "true")
    closed_env.setenv("AADS_CHAT_PROTOCOL_V2_ENABLED", "true")
    capabilities = chat_protocol.chat_protocol_capabilities()

    assert chat_protocol.CHAT_PROTOCOL_V2_CAPABILITY not in capabilities["capabilities"]
    assert capabilities["production_ready"] is False
    assert capabilities["activation_gates"]["stable_generation_identity"] is True
    assert capabilities["activation_gates"]["wp04_read_model_migration"] is False
    assert "wp04_read_model_migration" in capabilities["pending_activation_requires"]


def test_every_gate_open_advertises_v2_without_changing_the_default(closed_env):
    _open_every_gate(closed_env)
    capabilities = chat_protocol.chat_protocol_capabilities()

    assert chat_protocol.chat_protocol_v2_activation_ready() is True
    assert chat_protocol.CHAT_PROTOCOL_V2_CAPABILITY in capabilities["capabilities"]
    assert capabilities["production_ready"] is True
    assert capabilities["pending_activation_requires"] == []
    # Advertising v2 must never change what an unversioned client receives.
    assert capabilities["default_contract_version"] == chat_protocol.CHAT_CONTRACT_V1
    assert chat_protocol.negotiate_contract_version(None) == chat_protocol.CHAT_CONTRACT_V1
    assert capabilities["legacy_compatibility"]["unwrapped_sse_events"] is True


def test_advertisement_helper_matches_discovery_payload(closed_env):
    _open_every_gate(closed_env)
    assert (
        chat_protocol.advertised_chat_capabilities()
        == chat_protocol.chat_protocol_capabilities()["capabilities"]
    )

    closed_env.delenv("AADS_CHAT_V2_BROWSER_CONTRACT_VERIFIED", raising=False)
    assert (
        chat_protocol.CHAT_PROTOCOL_V2_CAPABILITY
        not in chat_protocol.advertised_chat_capabilities()
    )


def test_streaming_status_carries_the_capability_advertisement(closed_env):
    # The browser reads the advertisement from the status it already polls.
    assert StreamingStatusOut().capabilities == []

    _open_every_gate(closed_env)
    payload = StreamingStatusOut(
        capabilities=chat_protocol.advertised_chat_capabilities()
    )
    assert chat_protocol.CHAT_PROTOCOL_V2_CAPABILITY in payload.capabilities


def test_discovery_model_serializes_the_new_gate_fields(closed_env):
    _open_every_gate(closed_env)
    model = ChatProtocolCapabilitiesOut(**chat_protocol.chat_protocol_capabilities())
    dumped = model.model_dump()

    assert dumped["activation_gates"]["cross_version_browser_contract_tests"] is True
    assert dumped["pending_activation_requires"] == []
    assert chat_protocol.CHAT_PROTOCOL_V2_CAPABILITY in dumped["capabilities"]
