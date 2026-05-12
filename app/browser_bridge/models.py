"""Core Browser Bridge data models.

These models deliberately avoid provider-specific AADS chat concepts so the same
session registry can be reused by API tools, local bridge agents, and E2E checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BrowserEndpointKind(str, Enum):
    """Supported browser session attachment styles."""

    CDP = "cdp"
    WEBSOCKET = "websocket"
    LOCAL_AGENT = "local_agent"
    STORAGE_STATE = "storage_state"
    HEADLESS = "headless"


@dataclass
class BrowserEndpoint:
    """Connection information for a browser bridge session."""

    kind: BrowserEndpointKind
    url: Optional[str] = None
    browser_name: str = "chromium"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "url": self.url,
            "browser_name": self.browser_name,
            "metadata": dict(self.metadata or {}),
        }


@dataclass
class PairingTokenRecord:
    """Hashed one-time pairing token record."""

    pairing_id: str
    token_hash: str
    label: str
    created_at: datetime
    expires_at: datetime
    created_by: str = ""
    consumed_at: Optional[datetime] = None

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def is_expired(self) -> bool:
        return utcnow() >= self.expires_at


@dataclass
class BrowserBridgeSession:
    """Registered browser session usable by tools or E2E."""

    session_id: str
    label: str
    endpoint: BrowserEndpoint
    registered_at: datetime
    pairing_id: str = ""
    storage_state_ref: Optional[str] = None
    created_by: str = ""
    active: bool = False
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    lease_owner: str = ""
    lease_expires_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and utcnow() >= self.expires_at

    def mark_used(self) -> None:
        self.last_used_at = utcnow()

    def public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "label": self.label,
            "endpoint": self.endpoint.public_dict(),
            "registered_at": self.registered_at.isoformat(),
            "active": self.active,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "has_storage_state": bool(self.storage_state_ref),
            "lease_owner": self.lease_owner or None,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "leased": bool(self.lease_owner and self.lease_expires_at and self.lease_expires_at > utcnow()),
        }


@dataclass
class PairingCreated:
    """Return object for a newly created one-time pairing token."""

    pairing_id: str
    token: str
    expires_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "pairing_id": self.pairing_id,
            "pairing_token": self.token,
            "expires_at": self.expires_at.isoformat(),
        }
