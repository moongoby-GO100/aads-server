"""Security helpers for Browser Bridge registration."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .models import BrowserEndpointKind

_DEFAULT_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_CDP_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_WEBSOCKET_SCHEMES = frozenset({"ws", "wss"})


class BrowserBridgeSecurityError(ValueError):
    """Raised when a browser bridge endpoint violates local-only policy."""


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "..." + "*" * visible


def _configured_extra_loopback_hosts() -> set[str]:
    raw = os.environ.get("AADS_BROWSER_BRIDGE_EXTRA_LOOPBACK_HOSTS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def is_loopback_host(hostname: str, extra_hosts: Iterable[str] | None = None) -> bool:
    host = (hostname or "").strip().strip("[]").lower()
    if not host:
        return False

    allowed_hosts = set(_DEFAULT_LOOPBACK_HOSTS)
    allowed_hosts.update(_configured_extra_loopback_hosts())
    if extra_hosts:
        allowed_hosts.update(h.strip().lower() for h in extra_hosts if h.strip())
    if host in allowed_hosts:
        return True

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_bridge_endpoint(kind: BrowserEndpointKind, url: str | None) -> None:
    """Validate that a browser endpoint is local-only by default."""
    if kind in {BrowserEndpointKind.LOCAL_AGENT, BrowserEndpointKind.STORAGE_STATE, BrowserEndpointKind.HEADLESS}:
        if not url:
            return

    if not url:
        raise BrowserBridgeSecurityError(f"{kind.value} endpoint requires url")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname or ""

    if parsed.username or parsed.password:
        raise BrowserBridgeSecurityError("browser endpoint URL must not include credentials")

    if kind == BrowserEndpointKind.WEBSOCKET and scheme not in _WEBSOCKET_SCHEMES:
        raise BrowserBridgeSecurityError(f"unsupported websocket endpoint scheme: {scheme}")

    if kind == BrowserEndpointKind.CDP and scheme not in _CDP_SCHEMES:
        raise BrowserBridgeSecurityError(f"unsupported CDP endpoint scheme: {scheme}")

    if kind not in {BrowserEndpointKind.CDP, BrowserEndpointKind.WEBSOCKET} and scheme not in _CDP_SCHEMES:
        raise BrowserBridgeSecurityError(f"unsupported browser endpoint scheme: {scheme}")

    if not is_loopback_host(hostname):
        raise BrowserBridgeSecurityError(
            "browser bridge endpoint must bind to localhost/loopback by default"
        )


def ensure_child_path(base_dir: Path, candidate: Path) -> Path:
    base = base_dir.resolve()
    target = candidate.resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise BrowserBridgeSecurityError("browser bridge state path escapes state directory") from exc
    return target
