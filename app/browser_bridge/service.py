"""Browser Bridge service and Playwright context adapter."""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any, Optional

from .models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind, PairingCreated, utcnow
from .registry import PairingManager, SessionRegistry, new_session_id
from .security import validate_bridge_endpoint
from .storage_state import StorageStateManager


class BrowserBridgeError(RuntimeError):
    pass


class BrowserBridgeService:
    """Reusable browser-session service for tools, APIs, and E2E adapters."""

    def __init__(
        self,
        pairings: PairingManager | None = None,
        sessions: SessionRegistry | None = None,
        storage_states: StorageStateManager | None = None,
    ):
        self.pairings = pairings or PairingManager()
        self.sessions = sessions or SessionRegistry()
        self.storage_states = storage_states or StorageStateManager()
        self._context_lock: Optional[asyncio.Lock] = None
        self._pw_handle: Any = None
        self._headless_browser: Any = None
        self._headless_context: Any = None
        self._session_contexts: dict[str, Any] = {}
        self._session_browsers: dict[str, Any] = {}

    def create_pairing(
        self,
        label: str = "CEO local Chrome",
        created_by: str = "",
        ttl_seconds: int = 600,
    ) -> PairingCreated:
        return self.pairings.create_pairing(label=label, created_by=created_by, ttl_seconds=ttl_seconds)

    def register_session(
        self,
        *,
        pairing_token: str,
        label: str,
        endpoint_kind: str,
        endpoint_url: str | None = None,
        browser_name: str = "chromium",
        storage_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        activate: bool = True,
        expires_hours: int | None = None,
    ) -> BrowserBridgeSession:
        endpoint = BrowserEndpoint(
            kind=BrowserEndpointKind(endpoint_kind),
            url=endpoint_url,
            browser_name=browser_name or "chromium",
            metadata=metadata or {},
        )
        validate_bridge_endpoint(endpoint.kind, endpoint.url)

        pairing = self.pairings.consume(pairing_token)
        session_id = new_session_id()
        storage_state_ref = None
        if storage_state is not None:
            storage_state_ref = self.storage_states.save(session_id, storage_state)

        expires_at = utcnow() + timedelta(hours=expires_hours) if expires_hours else None
        session = BrowserBridgeSession(
            session_id=session_id,
            label=label or pairing.label or "Browser Bridge Session",
            endpoint=endpoint,
            registered_at=utcnow(),
            pairing_id=pairing.pairing_id,
            storage_state_ref=storage_state_ref,
            created_by=pairing.created_by,
            expires_at=expires_at,
        )
        return self.sessions.register(session, activate=activate)

    def select_session(self, session_id: str) -> BrowserBridgeSession:
        return self.sessions.select(session_id)

    def active_session(self) -> BrowserBridgeSession | None:
        return self.sessions.get_active()

    async def acquire_playwright_context(
        self,
        session_id: str | None = None,
    ) -> tuple[Any, Optional[str]]:
        """Return a bridge Playwright context or a headless fallback context.

        When session_id is provided, that exact Browser Bridge session is used
        and the global active session is not changed. This lets concurrent
        jobs pin themselves to different local browser sessions.
        """
        if self._context_lock is None:
            self._context_lock = asyncio.Lock()
        async with self._context_lock:
            try:
                session = self.sessions.get(session_id) if session_id else self.sessions.get_active()
                if session_id and not session:
                    raise BrowserBridgeError(f"browser bridge session not found: {session_id}")
                if session:
                    context = await self._context_for_session(session)
                    session.mark_used()
                    return context, None
                context = await self._headless_fallback_context()
                return context, None
            except Exception as exc:
                return None, f"[브라우저 도구 사용 불가] {exc}"

    async def _ensure_playwright(self) -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserBridgeError("playwright 패키지가 설치되지 않았습니다") from exc

        if self._pw_handle is None:
            self._pw_handle = await async_playwright().start()
        return self._pw_handle

    async def _headless_fallback_context(self) -> Any:
        pw = await self._ensure_playwright()
        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            for candidate in ("/root/.cache/ms-playwright", "/root/.cache"):
                if os.path.isdir(os.path.join(candidate, "chromium-1208")) or os.path.isdir(
                    os.path.join(candidate, "chromium_headless_shell-1208")
                ):
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = candidate
                    break

        need_init = (
            self._headless_context is None
            or self._headless_browser is None
            or not self._headless_browser.is_connected()
        )
        if need_init:
            self._headless_browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--memory-pressure-off",
                ],
            )
            self._headless_context = await self._headless_browser.new_context(
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
            )
        return self._headless_context

    async def _context_for_session(self, session: BrowserBridgeSession) -> Any:
        cached = self._session_contexts.get(session.session_id)
        browser = self._session_browsers.get(session.session_id)
        if cached is not None and (browser is None or browser.is_connected()):
            return cached

        pw = await self._ensure_playwright()
        endpoint = session.endpoint
        if endpoint.kind == BrowserEndpointKind.CDP:
            browser = await pw.chromium.connect_over_cdp(endpoint.url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
        elif endpoint.kind == BrowserEndpointKind.WEBSOCKET:
            browser = await pw.chromium.connect(endpoint.url)
            context = await browser.new_context()
        elif endpoint.kind == BrowserEndpointKind.STORAGE_STATE:
            storage_state = None
            if session.storage_state_ref:
                storage_state = self.storage_states.path_for_playwright(session.storage_state_ref)
            browser = await pw.chromium.launch(headless=True)
            context_kwargs = {
                "viewport": {"width": 1280, "height": 720},
                "java_script_enabled": True,
            }
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            context = await browser.new_context(**context_kwargs)
        elif endpoint.kind == BrowserEndpointKind.LOCAL_AGENT:
            raise BrowserBridgeError("local_agent command proxy is registered but not connected")
        else:
            return await self._headless_fallback_context()

        self._session_browsers[session.session_id] = browser
        self._session_contexts[session.session_id] = context
        return context

    def e2e_config(self, session_id: str | None = None) -> dict[str, Any]:
        session = self.sessions.get(session_id) if session_id else self.sessions.get_active()
        if session_id and not session:
            return {
                "mode": "unavailable",
                "session_id": session_id,
                "headless_fallback": False,
                "error": f"browser bridge session not found: {session_id}",
            }
        if not session:
            return {"mode": "headless", "session_id": None, "headless_fallback": True}

        config: dict[str, Any] = {
            "mode": session.endpoint.kind.value,
            "session_id": session.session_id,
            "headless_fallback": True,
        }
        if session.endpoint.kind == BrowserEndpointKind.CDP:
            config["endpoint_url"] = session.endpoint.url
            config["cdp_url"] = session.endpoint.url
        elif session.endpoint.kind == BrowserEndpointKind.WEBSOCKET:
            config["endpoint_url"] = session.endpoint.url
            config["ws_url"] = session.endpoint.url
        if session.storage_state_ref:
            config["storage_state_path"] = self.storage_states.path_for_playwright(
                session.storage_state_ref
            )
        return config


_service: BrowserBridgeService | None = None


def get_browser_bridge_service() -> BrowserBridgeService:
    global _service
    if _service is None:
        _service = BrowserBridgeService()
    return _service
