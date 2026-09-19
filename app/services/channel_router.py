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
    "screenshot_ocr", "downloaded_file", "file", "rag", "RAG",
})
UNTRUSTED_PAGE_DATA = "UNTRUSTED_PAGE_DATA"
PAGE_DATA_COMMAND_ATTEMPT = "PAGE_DATA_COMMAND_ATTEMPT"
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
    # This is issued by the ingress adapter after authentication.  It is not a
    # client supplied ``source`` label: the router binds it to tenant and user.
    authenticated_provenance: Mapping[str, Any]


@dataclass(frozen=True)
class ObservationEnvelope:
    source: str
    tenant_id: str
    session_id: str
    correlation_id: str
    trust_level: str
    payload: Mapping[str, Any]
    payload_hash: str
    taint: str = UNTRUSTED_PAGE_DATA


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
    authenticated_provenance: Mapping[str, Any]


def directive_from_authenticated_context(
    context: Mapping[str, Any], *, session_id: str, correlation_id: str,
    payload: Mapping[str, Any], capabilities: frozenset[str],
    source: str = "user_directive",
) -> DirectiveEnvelope:
    """Create command provenance from server-authenticated request context.

    API callers never choose the effective tenant, user, or trust level.  The
    only supported public ingress is an authenticated user directive; internal
    callers must construct their own server-attested context deliberately.
    """
    tenant = context.get("tenant") if isinstance(context, Mapping) else None
    membership = context.get("membership") if isinstance(context, Mapping) else None
    tenant_id = str((tenant or {}).get("id") or "")
    user_id = str((membership or {}).get("user_id") or "")
    provenance = {
        "issuer": "server_authenticated_request",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "source": source,
    }
    return DirectiveEnvelope(
        source=source,
        tenant_id=tenant_id,
        session_id=session_id,
        correlation_id=correlation_id,
        trust_level="trusted" if source != "internal_control" else "internal",
        allowed_capabilities=capabilities,
        payload=payload,
        payload_hash=payload_hash(payload),
        authenticated_provenance=provenance,
    )


class ChannelRouter:
    """Validate every ingress before it reaches a browser action path."""

    def route_directive(self, envelope: DirectiveEnvelope, *, capability: str) -> ActionIntent:
        # Required fields have a stable fail-closed contract independent of
        # source classification. A complete page-derived source still receives
        # the more specific PAGE_DATA_COMMAND_ATTEMPT reason below.
        self._validate_common(envelope, capability)
        if envelope.source in OBSERVATION_SOURCES:
            self._reject(envelope, PAGE_DATA_COMMAND_ATTEMPT, capability)
        if envelope.source not in TRUSTED_COMMAND_SOURCES:
            self._reject(envelope, "UNTRUSTED_COMMAND_SOURCE", capability)
        self._validate_authenticated_provenance(envelope, capability)
        self.assert_no_untrusted_page_data(envelope.payload, envelope=envelope, capability=capability)
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
            authenticated_provenance=envelope.authenticated_provenance,
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
                authenticated_provenance=intent.authenticated_provenance,
            ),
            capability=capability,
        )

    def route_observation(self, envelope: ObservationEnvelope) -> ObservationEnvelope:
        self._validate_common(envelope, "observation.ingest")
        if envelope.source not in OBSERVATION_SOURCES:
            self._reject(envelope, "INVALID_OBSERVATION_SOURCE", "observation.ingest")
        if envelope.trust_level != "untrusted":
            self._reject(envelope, "OBSERVATION_TRUST_MISMATCH", "observation.ingest")
        if envelope.taint != UNTRUSTED_PAGE_DATA:
            self._reject(envelope, "OBSERVATION_TAINT_MISMATCH", "observation.ingest")
        self._audit(envelope, "accepted", UNTRUSTED_PAGE_DATA, "observation.ingest")
        return envelope

    def assert_no_untrusted_page_data(
        self, value: Any, *, envelope: Any | None = None, capability: str = "execution"
    ) -> None:
        """Fail closed if an observation reaches a command/tool argument.

        The marker is data-model provenance, not a text tag, so page text such
        as ``</untrusted_page_content>`` cannot break out of this boundary.
        """
        if _contains_untrusted_page_data(value):
            target = envelope or _AuditEnvelope()
            self._reject(target, PAGE_DATA_COMMAND_ATTEMPT, capability)

    def _validate_authenticated_provenance(self, envelope: DirectiveEnvelope, capability: str) -> None:
        provenance = envelope.authenticated_provenance
        if not isinstance(provenance, Mapping):
            self._reject(envelope, "MISSING_AUTHENTICATED_PROVENANCE", capability)
        if provenance.get("issuer") != "server_authenticated_request":
            self._reject(envelope, "UNAUTHENTICATED_PROVENANCE", capability)
        if str(provenance.get("tenant_id") or "") != envelope.tenant_id:
            self._reject(envelope, "PROVENANCE_TENANT_MISMATCH", capability)
        if str(provenance.get("source") or "") != envelope.source:
            self._reject(envelope, "PROVENANCE_SOURCE_MISMATCH", capability)

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


@dataclass(frozen=True)
class _AuditEnvelope:
    source: str = "untrusted_page_data"
    tenant_id: str = ""
    session_id: str = ""
    correlation_id: str = ""
    payload_hash: str = ""


def _contains_untrusted_page_data(value: Any) -> bool:
    if isinstance(value, ObservationEnvelope):
        return value.taint == UNTRUSTED_PAGE_DATA
    if isinstance(value, Mapping):
        if value.get("__aads_taint__") == UNTRUSTED_PAGE_DATA:
            return True
        return any(_contains_untrusted_page_data(item) for item in value.values())
    if isinstance(value, (tuple, list, frozenset, set)):
        return any(_contains_untrusted_page_data(item) for item in value)
    return False
