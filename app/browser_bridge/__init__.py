"""Reusable Browser Bridge core for local authenticated browser reuse."""
from .models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind
from .service import BrowserBridgeService, get_browser_bridge_service

__all__ = [
    "BrowserBridgeService",
    "BrowserBridgeSession",
    "BrowserEndpoint",
    "BrowserEndpointKind",
    "get_browser_bridge_service",
]
