"""Smart Browser command/observation boundary (v2.0 section 9.1).

This module deliberately has no executor. It only turns an authenticated,
hashed command envelope into an ``ActionIntent``; page-derived material remains
an ``ObservationEnvelope`` and can never acquire execution authority here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any, Mapping


logger = logging.getLogger(__name__)

TRUSTED_COMMAND_SOURCES = frozenset({"user_directive", "approved_recipe", "internal_control"})
OBSERVATION_SOURCES = frozenset({
    "page_text", "dom", "DOM", "aria", "ARIA", "ocr", "OCR",
    "screenshot_ocr", "downloaded_file",
})
_SOURCE_TRUST = {
    "user_directive": frozenset({"trusted"}),
    "approved_recipe": frozenset({"trusted"}),
    "internal_control": frozenset({"internal"}),
}


class ChannelRoutingError(ValueError):
    """Fail-closed routing error with a stable audit reason code."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(detail or reason_code)


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Canonical payload hash; never trust a client supplied hash value."""
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DirectiveEnvelope:
    source: str
    tenant_id: str
    session_id: str
    correlation_id: str
    trust_level: str
    allowed_capabilities: frozenset[str]
    payload: Mapping[str, Any]
    payload_hash: str


@dataclass(frozen=True)
class ObservationEnvelope:
    source: str
    tenant_id: str
    session_id: str
    correlation_id: str
    trust_level: str
    payload: Mapping[str, Any]
    payload_hash: str


@dataclass(frozen=True)
class ActionIntent:
    source: str
    tenant_id: str
    session_id: str
    correlation_id: str
    trust_level: str
    allowed_capabilities: frozenset[str]
    payload: Mapping[str, Any]
    payload_hash: str


class ChannelRouter:
    """Validate every ingress before it reaches a browser action path."""

    def route_directive(self, envelope: DirectiveEnvelope, *, capability: str) -> ActionIntent:
        # Required fields have a stable fail-closed contract independent of
        # source classification. A complete page-derived source still receives
        # the more specific PAGE_DATA_COMMAND_ATTEMPT reason below.
        self._validate_common(envelope, capability)
        if envelope.source in OBSERVATION_SOURCES:
            self._reject(envelope, "PAGE_DATA_COMMAND_ATTEMPT", capability)
        if envelope.source not in TRUSTED_COMMAND_SOURCES:
            self._reject(envelope, "UNTRUSTED_COMMAND_SOURCE", capability)
        if envelope.trust_level not in _SOURCE_TRUST[envelope.source]:
            self._reject(envelope, "TRUST_LEVEL_MISMATCH", capability)
        if capability not in envelope.allowed_capabilities:
            self._reject(envelope, "CAPABILITY_NOT_ALLOWED", capability)
        self._audit(envelope, "accepted", "TRUSTED_COMMAND_CHANNEL", capability)
        return ActionIntent(
            source=envelope.source,
            tenant_id=envelope.tenant_id,
            session_id=envelope.session_id,
            correlation_id=envelope.correlation_id,
            trust_level=envelope.trust_level,
            allowed_capabilities=envelope.allowed_capabilities,
            payload=envelope.payload,
            payload_hash=envelope.payload_hash,
        )

    def validate_action_intent(self, intent: ActionIntent, *, capability: str) -> ActionIntent:
        """Re-check an intent at the executor boundary to prevent forged objects."""
        return self.route_directive(
            DirectiveEnvelope(
                source=intent.source,
                tenant_id=intent.tenant_id,
                session_id=intent.session_id,
                correlation_id=intent.correlation_id,
                trust_level=intent.trust_level,
                allowed_capabilities=intent.allowed_capabilities,
                payload=intent.payload,
                payload_hash=intent.payload_hash,
            ),
            capability=capability,
        )

    def route_observation(self, envelope: ObservationEnvelope) -> ObservationEnvelope:
        self._validate_common(envelope, "observation.ingest")
        if envelope.source not in OBSERVATION_SOURCES:
            self._reject(envelope, "INVALID_OBSERVATION_SOURCE", "observation.ingest")
        if envelope.trust_level != "untrusted":
            self._reject(envelope, "OBSERVATION_TRUST_MISMATCH", "observation.ingest")
        self._audit(envelope, "accepted", "UNTRUSTED_PAGE_DATA", "observation.ingest")
        return envelope

    def _validate_common(self, envelope: Any, capability: str) -> None:
        for field in ("source", "tenant_id", "session_id", "correlation_id", "payload_hash"):
            if not str(getattr(envelope, field, "") or "").strip():
                self._reject(envelope, f"MISSING_{field.upper()}", capability)
        if not isinstance(envelope.payload, Mapping):
            self._reject(envelope, "INVALID_PAYLOAD", capability)
        if payload_hash(envelope.payload) != envelope.payload_hash:
            self._reject(envelope, "PAYLOAD_HASH_MISMATCH", capability)

    def _reject(self, envelope: Any, reason_code: str, capability: str) -> None:
        self._audit(envelope, "blocked", reason_code, capability)
        raise ChannelRoutingError(reason_code)

    @staticmethod
    def _audit(envelope: Any, decision: str, reason_code: str, capability: str) -> None:
        logger.info(
            "channel_router_decision",
            extra={
                "decision": decision,
                "reason_code": reason_code,
                "source": getattr(envelope, "source", ""),
                "tenant_id": getattr(envelope, "tenant_id", ""),
                "session_id": getattr(envelope, "session_id", ""),
                "correlation_id": getattr(envelope, "correlation_id", ""),
                "capability": capability,
                "payload_hash": getattr(envelope, "payload_hash", ""),
            },
        )
