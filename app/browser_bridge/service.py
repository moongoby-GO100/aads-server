"""Browser Bridge service and Playwright context adapter."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Any, Optional

from .models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind, PairingCreated, utcnow
from .registry import PairingManager, SessionRegistry, new_session_id
from .security import validate_bridge_endpoint
from .storage_state import StorageStateManager


class BrowserBridgeError(RuntimeError):
    pass


logger = logging.getLogger(__name__)

LOCAL_AGENT_QUEUE_WAIT_SECONDS = 60
LOCAL_AGENT_COMMAND_TIMEOUT_SECONDS = 120
LOCAL_AGENT_NAVIGATION_TIMEOUT_SECONDS = 180
LOCAL_AGENT_SNAPSHOT_TIMEOUT_SECONDS = 180
LOCAL_AGENT_LEASE_BUFFER_SECONDS = 30


class _LocalAgentLocator:
    def __init__(self, page: "_LocalAgentPage", selector: str):
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "_LocalAgentLocator":
        return self

    async def aria_snapshot(self) -> str:
        text = await self._page._run_browser_command(
            "browser_get_text",
            {"selector": self._selector if self._selector != "body" else ""},
        )
        return str(text.get("text") or "")

    async def click(self, **_: Any) -> None:
        await self._page.click(self._selector)

    async def fill(self, value: str, **_: Any) -> None:
        await self._page.fill(self._selector, value)

    async def clear(self, **_: Any) -> None:
        await self._page.fill(self._selector, "")


class _LocalAgentPage:
    """Small Playwright-like facade backed by PC Agent browser commands."""

    def __init__(self, session: BrowserBridgeSession, service: "BrowserBridgeService"):
        self._session = session
        self._service = service
        metadata = dict(session.endpoint.metadata or {})
        self._agent_id = str(metadata.get("agent_id") or "")
        self._port = int(metadata.get("port") or 9222)
        self.url = str(metadata.get("last_url") or "about:blank")

    def _params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(params or {})
        payload.setdefault("port", self._port)
        return payload

    async def _run_browser_command(
        self,
        command_type: str,
        params: dict[str, Any] | None = None,
        *,
        command_timeout_seconds: float = LOCAL_AGENT_COMMAND_TIMEOUT_SECONDS,
        queue_wait_timeout_seconds: float = LOCAL_AGENT_QUEUE_WAIT_SECONDS,
    ) -> dict[str, Any]:
        from app.services.pc_agent_manager import pc_agent_manager

        lease_ttl_seconds = int(command_timeout_seconds + LOCAL_AGENT_LEASE_BUFFER_SECONDS)
        result = await pc_agent_manager.execute_routed_command(
            command_type=command_type,
            params=self._params(params),
            agent_id=self._agent_id,
            job_type=f"browser_bridge_{self._session.session_id}",
            required_capabilities=["interactive_browser"],
            queue_if_busy=True,
            wait_for_turn=True,
            queue_wait_timeout_seconds=queue_wait_timeout_seconds,
            lease_ttl_seconds=lease_ttl_seconds,
            command_timeout_seconds=command_timeout_seconds,
        )
        if result.get("status") != "success" and str(result.get("error_code") or "") == "PC_AGENT_OFFLINE":
            active_result = await self._service._execute_pc_agent_route_via_active_api(
                command_type=command_type,
                params=self._params(params),
                agent_id=self._agent_id,
                job_type=f"browser_bridge_{self._session.session_id}",
                required_capabilities=["interactive_browser"],
                queue_wait_timeout_seconds=queue_wait_timeout_seconds,
                lease_ttl_seconds=lease_ttl_seconds,
                command_timeout_seconds=command_timeout_seconds,
            )
            if active_result is not None:
                result = active_result
        if result.get("status") != "success":
            raise BrowserBridgeError(str(result.get("message") or result.get("error_code") or result))
        command_result = result.get("result") if isinstance(result, dict) else None
        data = command_result.get("result") if isinstance(command_result, dict) else None
        if data is None and isinstance(command_result, dict):
            data = command_result.get("data")
        if data is None and isinstance(result, dict):
            data = result.get("data")
        if isinstance(data, dict):
            if data.get("error"):
                raise BrowserBridgeError(str(data.get("error")))
            return data
        return {}

    async def goto(self, url: str, **_: Any) -> None:
        await self._run_browser_command(
            "browser_navigate",
            {"url": url},
            command_timeout_seconds=LOCAL_AGENT_NAVIGATION_TIMEOUT_SECONDS,
        )
        self.url = url
        metadata = dict(self._session.endpoint.metadata or {})
        metadata["last_url"] = url
        self._session.endpoint.metadata = metadata

    async def title(self) -> str:
        data = await self._run_browser_command(
            "browser_eval",
            {"expression": "document.title"},
            command_timeout_seconds=LOCAL_AGENT_SNAPSHOT_TIMEOUT_SECONDS,
        )
        return str(data.get("value") or "")

    def locator(self, selector: str) -> _LocalAgentLocator:
        return _LocalAgentLocator(self, selector)

    async def evaluate(self, expression: str) -> Any:
        data = await self._run_browser_command(
            "browser_eval",
            {"expression": expression},
            command_timeout_seconds=LOCAL_AGENT_SNAPSHOT_TIMEOUT_SECONDS,
        )
        value = data.get("value")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    async def screenshot(self, **_: Any) -> bytes:
        data = await self._run_browser_command(
            "browser_screenshot",
            {"format": "png"},
            command_timeout_seconds=LOCAL_AGENT_SNAPSHOT_TIMEOUT_SECONDS,
        )
        encoded = str(data.get("screenshot_base64") or "")
        return base64.b64decode(encoded.encode("ascii")) if encoded else b""

    async def click(self, selector: str, **_: Any) -> None:
        await self._run_browser_command("browser_click", {"selector": selector})

    async def fill(self, selector: str, value: str, **_: Any) -> None:
        await self._run_browser_command("browser_fill", {"selector": selector, "value": value})

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(max(0, ms) / 1000)

    async def close(self) -> None:
        return None


class _LocalAgentContext:
    def __init__(self, session: BrowserBridgeSession, service: "BrowserBridgeService"):
        self._session = session
        self._page = _LocalAgentPage(session, service)

    @property
    def pages(self) -> list[_LocalAgentPage]:
        return [self._page]

    async def new_page(self) -> _LocalAgentPage:
        return self._page


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

    def register_trusted_session(
        self,
        *,
        label: str,
        endpoint_kind: str,
        endpoint_url: str | None = None,
        browser_name: str = "chromium",
        metadata: dict[str, Any] | None = None,
        activate: bool = False,
        expires_hours: int | None = None,
        session_id: str | None = None,
        created_by: str = "system",
    ) -> BrowserBridgeSession:
        endpoint = BrowserEndpoint(
            kind=BrowserEndpointKind(endpoint_kind),
            url=endpoint_url,
            browser_name=browser_name or "chromium",
            metadata=metadata or {},
        )
        validate_bridge_endpoint(endpoint.kind, endpoint.url)
        expires_at = utcnow() + timedelta(hours=expires_hours) if expires_hours else None
        session = BrowserBridgeSession(
            session_id=session_id or new_session_id(),
            label=label or "Browser Bridge Session",
            endpoint=endpoint,
            registered_at=utcnow(),
            created_by=created_by,
            expires_at=expires_at,
        )
        return self.sessions.register(session, activate=activate)

    async def ensure_pc_agent_cdp_session(
        self,
        *,
        agent_id: str = "",
        label: str = "PC Agent Chrome",
        url: str = "about:blank",
        preferred_port: int | None = None,
        isolated_profile: bool = True,
        isolation_id: str = "",
        activate: bool = False,
    ) -> BrowserBridgeSession:
        """Launch or reuse Chrome through PC Agent and register a local-agent bridge session."""
        from app.services.pc_agent_manager import pc_agent_manager

        launch_params: dict[str, Any] = {
            "url": url or "about:blank",
            "dynamic_port": True,
            "isolated_profile": isolated_profile,
            "new_window": True,
        }
        if preferred_port:
            launch_params["preferred_port"] = int(preferred_port)
        if isolation_id:
            launch_params["isolation_id"] = isolation_id

        routed = await pc_agent_manager.execute_routed_command(
            command_type="browser_launch",
            params=launch_params,
            agent_id=agent_id,
            job_type="browser_bridge_launch",
            required_capabilities=["interactive_browser"],
            queue_if_busy=True,
            wait_for_turn=True,
            queue_wait_timeout_seconds=60,
            lease_ttl_seconds=120,
            command_timeout_seconds=90,
        )
        if routed.get("status") != "success" and str(routed.get("error_code") or "") == "PC_AGENT_OFFLINE":
            active_routed = await self._execute_pc_agent_route_via_active_api(
                command_type="browser_launch",
                params=launch_params,
                agent_id=agent_id,
                job_type="browser_bridge_launch",
                required_capabilities=["interactive_browser"],
            )
            if active_routed is not None:
                routed = active_routed
        if routed.get("status") != "success":
            raise BrowserBridgeError(str(routed.get("message") or routed.get("error_code") or routed))

        lease = routed.get("lease") or {}
        selected_agent_id = str(lease.get("agent_id") or agent_id or "")
        if not selected_agent_id:
            raise BrowserBridgeError("PC Agent browser_launch did not return agent_id")
        command_result = routed.get("result") or {}
        data = command_result.get("result") if isinstance(command_result, dict) else None
        if not isinstance(data, dict):
            raise BrowserBridgeError("PC Agent browser_launch returned no data")
        port = int(data.get("port") or preferred_port or 9222)
        existing = self.sessions.find_by_metadata(
            agent_id=selected_agent_id,
            port=str(port),
            endpoint_kind=BrowserEndpointKind.LOCAL_AGENT.value,
        )
        metadata = {
            "agent_id": selected_agent_id,
            "port": str(port),
            "profile_dir": str(data.get("user_data_dir") or ""),
            "websocket_debugger_url": str(data.get("websocket_debugger_url") or ""),
            "endpoint_kind": BrowserEndpointKind.LOCAL_AGENT.value,
            "cdp_url": f"pc-agent://{selected_agent_id}/cdp/{port}",
            "last_url": url or "about:blank",
        }
        if existing:
            existing.endpoint.metadata = metadata
            existing.label = label or existing.label
            self.sessions.touch(existing)
            if activate:
                return self.select_session(existing.session_id)
            return existing
        return self.register_trusted_session(
            label=label,
            endpoint_kind=BrowserEndpointKind.LOCAL_AGENT.value,
            metadata=metadata,
            activate=activate,
            created_by="pc_agent",
        )

    async def _execute_pc_agent_route_via_active_api(
        self,
        *,
        command_type: str,
        params: dict[str, Any],
        agent_id: str,
        job_type: str,
        required_capabilities: list[str],
        queue_wait_timeout_seconds: float = 60,
        lease_ttl_seconds: int = 120,
        command_timeout_seconds: float = 90,
    ) -> dict[str, Any] | None:
        """Fallback for tool processes that do not own the PC Agent websocket."""
        active_ports = self._active_api_ports()
        if not active_ports:
            return None

        payload = {
            "command_type": command_type,
            "params": params,
            "agent_id": agent_id,
            "job_type": job_type,
            "required_capabilities": required_capabilities,
            "queue_if_busy": True,
            "wait_for_turn": True,
            "queue_wait_timeout_seconds": queue_wait_timeout_seconds,
            "lease_ttl_seconds": lease_ttl_seconds,
            "command_timeout_seconds": command_timeout_seconds,
        }
        urls: list[str] = []
        for active_port in active_ports:
            urls.extend(self._active_api_route_urls(active_port))

        deduped_urls: list[str] = []
        for url in urls:
            if url not in deduped_urls:
                deduped_urls.append(url)

        def _post() -> dict[str, Any] | None:
            body = json.dumps(payload).encode("utf-8")
            for url in deduped_urls:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=max(95, command_timeout_seconds + 5)) as resp:
                        raw = resp.read().decode("utf-8")
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    logger.warning("browser_bridge_active_pc_agent_fallback_failed url=%s err=%s", url, exc)
                    continue
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.warning("browser_bridge_active_pc_agent_fallback_bad_json url=%s err=%s", url, exc)
                    continue
            return None

        return await asyncio.to_thread(_post)

    @classmethod
    def _active_api_ports(cls) -> list[str]:
        ports: list[str] = []
        active_port = cls._active_api_port()
        for candidate in (active_port, "8100", "8102"):
            if candidate and candidate.isdigit() and candidate not in ports:
                ports.append(candidate)
        return ports

    @classmethod
    def _active_api_route_urls(cls, active_port: str) -> list[str]:
        urls = [f"http://127.0.0.1:{active_port}/api/v1/pc-agent/route-execute"]
        if active_port == "8100":
            urls.append("http://aads-server:8080/api/v1/pc-agent/route-execute")
        elif active_port == "8102":
            urls.append("http://aads-server-green:8080/api/v1/pc-agent/route-execute")
        active_container = cls._active_container_name()
        if active_container:
            urls.append(f"http://{active_container}:8080/api/v1/pc-agent/route-execute")
        deduped: list[str] = []
        for url in urls:
            if url not in deduped:
                deduped.append(url)
        return deduped

    @staticmethod
    def _active_api_port() -> str:
        for candidate in (
            os.getenv("AADS_ACTIVE_PORT", ""),
            "/root/aads/aads-server/.active_port",
            ".active_port",
        ):
            value = candidate.strip()
            if not value:
                continue
            if value.isdigit():
                return value
            try:
                with open(value, "r", encoding="utf-8") as fh:
                    port = fh.read().strip()
            except OSError:
                continue
            if port.isdigit():
                return port
        return ""

    @staticmethod
    def _active_container_name() -> str:
        for candidate in (
            os.getenv("AADS_ACTIVE_CONTAINER", ""),
            "/root/aads/aads-server/.active_container",
            "/app/.active_container",
            ".active_container",
        ):
            value = candidate.strip()
            if not value:
                continue
            if "/" not in value and "." not in value:
                return value
            try:
                with open(value, "r", encoding="utf-8") as fh:
                    name = fh.read().strip()
            except OSError:
                continue
            if name:
                return name
        return ""


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
                    self.sessions.touch(session)
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

        endpoint = session.endpoint
        if endpoint.kind == BrowserEndpointKind.LOCAL_AGENT:
            context = _LocalAgentContext(session, self)
            self._session_contexts[session.session_id] = context
            return context

        pw = await self._ensure_playwright()
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
        elif session.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT:
            metadata = dict(session.endpoint.metadata or {})
            config["agent_id"] = metadata.get("agent_id")
            config["port"] = metadata.get("port")
            config["cdp_url"] = metadata.get("cdp_url")
            config["headless_fallback"] = False
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
