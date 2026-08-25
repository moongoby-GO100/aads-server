"""Browser Bridge service and Playwright context adapter."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Any, Optional

from .models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind, PairingCreated, utcnow
from .registry import PairingManager, SessionRegistry, new_session_id
from .security import validate_bridge_endpoint
from .storage_state import StorageStateManager


class BrowserBridgeError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "", detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_code = str(error_code or "")
        self.detail = dict(detail or {})


logger = logging.getLogger(__name__)

LOCAL_AGENT_QUEUE_WAIT_SECONDS = 60
LOCAL_AGENT_COMMAND_TIMEOUT_SECONDS = 120
LOCAL_AGENT_NAVIGATION_TIMEOUT_SECONDS = 180
LOCAL_AGENT_SNAPSHOT_TIMEOUT_SECONDS = 180
LOCAL_AGENT_LEASE_BUFFER_SECONDS = 30
WORK_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,119}$")
PROTECTED_WORK_KEYS = {"ntv2-sinsang-registration"}
PROTECTED_LABEL_MARKERS = ("sinsang", "신상마켓")
DEFAULT_WORK_SESSION_LABELS = {
    "ntv2-sinsang-registration": "NTV2 Sinsang registration",
    "ntv2-china-sourcing-admin": "NTV2 China sourcing admin",
    "ntv2-vvic-scrape": "NTV2 VVIC scrape",
}
LOCAL_AGENT_RECOVERABLE_ERROR_CODES = {"CDP_NOT_READY", "RUNTIME_EVALUATE_TIMEOUT", "STALE_TARGET", "COMMAND_TIMEOUT"}
LOCAL_AGENT_JS_COMMANDS = {
    "browser_click",
    "browser_fill",
    "browser_press_key",
    "browser_select_option",
    "browser_check",
    "browser_get_text",
    "browser_eval",
}
SIDECAR_SERVICE_ROLES = {
    "yeoljeong-finance-worker",
}
SIDECAR_QUEUE_WAIT_SECONDS = 20
SIDECAR_COMMAND_TIMEOUT_SECONDS = 90
SIDECAR_NAVIGATION_TIMEOUT_SECONDS = 150
SIDECAR_LAUNCH_TIMEOUT_SECONDS = 120
SIDECAR_SNAPSHOT_TIMEOUT_SECONDS = 90


def normalize_work_key(work_key: str) -> str:
    value = (work_key or "").strip().lower()
    if not value:
        raise ValueError("work_key required")
    if not WORK_KEY_PATTERN.match(value):
        raise ValueError("work_key must be 2-120 chars: lowercase letters, digits, dot, dash, underscore, colon")
    return value


def default_work_session_label(work_key: str) -> str:
    return DEFAULT_WORK_SESSION_LABELS.get(work_key, f"Browser work session: {work_key}")


def looks_like_protected_label(label: str) -> bool:
    normalized = (label or "").strip().lower()
    return any(marker in normalized for marker in PROTECTED_LABEL_MARKERS)


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
        self._recovered_error_codes: set[str] = set()
        self._sync_from_session(session)

    def _sync_from_session(self, session: BrowserBridgeSession | None = None) -> None:
        if session is not None:
            self._session = session
        metadata = dict(self._session.endpoint.metadata or {})
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

        recovery_attempted = False
        requested_url = str((params or {}).get("url") or self.url or "about:blank")

        while True:
            merged = self._params(params)
            if "work_key" not in merged and hasattr(self._session, "work_key") and self._session.work_key:
                merged["work_key"] = self._session.work_key
            if command_type in LOCAL_AGENT_JS_COMMANDS:
                merged.setdefault("evaluate_timeout_seconds", max(1.0, min(60.0, command_timeout_seconds - 0.5)))

            route_active_first = self._service._route_pc_agent_via_active_api_first()
            if route_active_first:
                queue_wait_timeout_seconds = min(
                    float(queue_wait_timeout_seconds),
                    self._service._sidecar_queue_wait_timeout_seconds(),
                )
                command_timeout_seconds = min(
                    float(command_timeout_seconds),
                    self._service._sidecar_command_timeout_seconds(command_type),
                )
            lease_ttl_seconds = int(command_timeout_seconds + LOCAL_AGENT_LEASE_BUFFER_SECONDS)
            if route_active_first:
                active_result = await self._service._execute_pc_agent_route_via_active_api(
                    command_type=command_type,
                    params=merged,
                    agent_id=self._agent_id,
                    job_type=f"browser_bridge_{self._session.session_id}",
                    required_capabilities=["interactive_browser"],
                    queue_wait_timeout_seconds=queue_wait_timeout_seconds,
                    lease_ttl_seconds=lease_ttl_seconds,
                    command_timeout_seconds=command_timeout_seconds,
                )
                result = active_result or {
                    "status": "error",
                    "error_code": "PC_AGENT_ROUTE_UNAVAILABLE",
                    "message": "active AADS API PC Agent route unavailable",
                }
            else:
                result = await pc_agent_manager.execute_routed_command(
                    command_type=command_type,
                    params=merged,
                    agent_id=self._agent_id,
                    job_type=f"browser_bridge_{self._session.session_id}",
                    required_capabilities=["interactive_browser"],
                    queue_if_busy=True,
                    wait_for_turn=True,
                    queue_wait_timeout_seconds=queue_wait_timeout_seconds,
                    lease_ttl_seconds=lease_ttl_seconds,
                    command_timeout_seconds=command_timeout_seconds,
                )
                if result.get("status") != "success" and (
                    str(result.get("error_code") or "") == "PC_AGENT_OFFLINE"
                    or os.path.exists("/.dockerenv")
                ):
                    active_result = await self._service._execute_pc_agent_route_via_active_api(
                        command_type=command_type,
                        params=merged,
                        agent_id=self._agent_id,
                        job_type=f"browser_bridge_{self._session.session_id}",
                        required_capabilities=["interactive_browser"],
                        queue_wait_timeout_seconds=queue_wait_timeout_seconds,
                        lease_ttl_seconds=lease_ttl_seconds,
                        command_timeout_seconds=command_timeout_seconds,
                    )
                    if active_result is not None:
                        result = active_result

            result = self._service._coerce_pc_agent_embedded_success(result)
            if result.get("status") == "success":
                command_result = result.get("result") if isinstance(result, dict) else None
                data = command_result.get("result") if isinstance(command_result, dict) else None
                if data is None and isinstance(command_result, dict):
                    data = command_result.get("data")
                if data is None and isinstance(result, dict):
                    data = result.get("data")
                if isinstance(data, dict):
                    if data.get("error"):
                        raise BrowserBridgeError(
                            str(data.get("error")),
                            error_code=str(data.get("error_code") or ""),
                            detail=data,
                        )
                    self._service._mark_local_agent_session_healthy(
                        self._session,
                        agent_id=self._agent_id,
                        port=self._port,
                        last_url=requested_url,
                    )
                    return data
                self._service._mark_local_agent_session_healthy(
                    self._session,
                    agent_id=self._agent_id,
                    port=self._port,
                    last_url=requested_url,
                )
                return {}

            error_code, error_message, error_detail = self._service._extract_pc_agent_route_error(result)
            if (
                not recovery_attempted
                and self._session.work_key
                and error_code in LOCAL_AGENT_RECOVERABLE_ERROR_CODES
                and error_code not in self._recovered_error_codes
            ):
                self._recovered_error_codes.add(error_code)
                recovered = await self._service._recover_local_agent_session(
                    self._session,
                    reason=error_code,
                    agent_id=self._agent_id,
                    preferred_port=self._port,
                    requested_url=requested_url,
                )
                if recovered is not None:
                    recovery_attempted = True
                    self._sync_from_session(recovered)
                    continue
            raise BrowserBridgeError(error_message, error_code=error_code, detail=error_detail)

    @staticmethod
    def _playwright_timeout_seconds(value: Any, default_seconds: float) -> float:
        try:
            timeout_ms = float(value)
        except (TypeError, ValueError):
            return default_seconds
        if timeout_ms <= 0:
            return default_seconds
        return max(1.0, min(default_seconds, timeout_ms / 1000.0))

    async def goto(self, url: str, **kwargs: Any) -> None:
        command_timeout_seconds = self._playwright_timeout_seconds(
            kwargs.get("timeout"),
            LOCAL_AGENT_NAVIGATION_TIMEOUT_SECONDS,
        )
        await self._run_browser_command(
            "browser_navigate",
            {"url": url},
            command_timeout_seconds=command_timeout_seconds,
        )
        # Try to capture the actual URL after potential server-side redirects
        # (e.g., AADS URL → /login). Without this, page.url always returns the
        # requested URL, so redirect detection in callers never fires.
        actual_url = url
        try:
            href_data = await self._run_browser_command(
                "browser_eval",
                {"expression": "window.location.href"},
                command_timeout_seconds=10.0,
            )
            href = str(href_data.get("value") or "").strip()
            if href.startswith("http"):
                actual_url = href
        except Exception:
            pass
        self.url = actual_url
        metadata = dict(self._session.endpoint.metadata or {})
        metadata["last_url"] = actual_url
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

    # Matches arrow/function expressions that must be called.
    # Distinguishes true arrow-function params ( `() =>`, `(a) =>`, `x =>` )
    # from IIFEs like `(function(){})()` or `(()=>{})()`  which start with `((`.
    _FUNC_EXPR_RE = re.compile(
        r"^(?:async\s+)?"
        r"(?:"
        r"\(\s*\)\s*=>"                        # no-arg: () =>
        r"|\(\s*[a-zA-Z_$][^)]*\)\s*=>"       # with-arg: (a, b) =>
        r"|[a-zA-Z_$][a-zA-Z0-9_$]*\s*=>"     # single-arg no-paren: x =>
        r"|function[\s(]"                       # function expression
        r")"
    )

    async def evaluate(self, expression: str, arg: Any = None, **kwargs: Any) -> Any:
        expr = expression.strip()
        command_timeout_seconds = self._playwright_timeout_seconds(
            kwargs.get("timeout"),
            LOCAL_AGENT_SNAPSHOT_TIMEOUT_SECONDS,
        )
        queue_wait_timeout_seconds = min(
            LOCAL_AGENT_QUEUE_WAIT_SECONDS,
            max(1.0, min(5.0, command_timeout_seconds / 3.0)),
        )
        if kwargs.get("timeout") is None and expr in {
            "window.location.href",
            "document.body ? document.body.innerHTML : ''",
            "document.title",
        }:
            command_timeout_seconds = min(command_timeout_seconds, 30.0)
        if arg is not None:
            # Keep sensitive arguments beyond the PC Agent's 200-character log preview.
            redacted_prefix = "/* browser-bridge argument redacted */" + (" " * 220)
            expr = f"{redacted_prefix}({expr})({json.dumps(arg)})"
        # Wrap no-argument arrow/function expressions as an IIFE to match
        # Playwright page.evaluate() semantics.
        elif self._FUNC_EXPR_RE.match(expr):
            expr = f"({expr})()"
        browser_params: dict[str, Any] = {"expression": expr}
        if kwargs.get("await_promise"):
            browser_params["await_promise"] = True
        data = await self._run_browser_command(
            "browser_eval",
            browser_params,
            command_timeout_seconds=command_timeout_seconds,
            queue_wait_timeout_seconds=queue_wait_timeout_seconds,
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

    async def press_key(self, key: str, selector: str = "") -> None:
        await self._run_browser_command("browser_press_key", {"key": key, "selector": selector})

    async def select_option(self, selector: str, value: str | list[str], **_: Any) -> None:
        await self._run_browser_command("browser_select_option", {"selector": selector, "value": value})

    async def set_checked(self, selector: str, checked: bool, **_: Any) -> None:
        await self._run_browser_command("browser_check", {"selector": selector, "checked": checked})

    async def set_input_files(self, selector: str, files: str | list[str], **_: Any) -> None:
        file_paths = files if isinstance(files, list) else [files]
        await self._run_browser_command("browser_file_upload", {"selector": selector, "file_paths": file_paths})

    async def download(self, selector: str, download_dir: str = "", timeout_seconds: float = 60) -> dict[str, Any]:
        return await self._run_browser_command(
            "browser_download",
            {"selector": selector, "download_dir": download_dir, "timeout_seconds": timeout_seconds},
            command_timeout_seconds=max(float(timeout_seconds) + 15, LOCAL_AGENT_COMMAND_TIMEOUT_SECONDS),
        )

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
        self._active_api_route_url_cache: str = ""

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
        work_key: str = "",
        protected: bool = False,
    ) -> BrowserBridgeSession:
        normalized_work_key = normalize_work_key(work_key) if work_key else ""
        is_protected = bool(protected or normalized_work_key in PROTECTED_WORK_KEYS or looks_like_protected_label(label))
        endpoint_metadata = dict(metadata or {})
        if normalized_work_key:
            endpoint_metadata["work_key"] = normalized_work_key
            endpoint_metadata["protected"] = is_protected
        endpoint = BrowserEndpoint(
            kind=BrowserEndpointKind(endpoint_kind),
            url=endpoint_url,
            browser_name=browser_name or "chromium",
            metadata=endpoint_metadata,
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
            work_key=normalized_work_key,
            protected=is_protected,
        )
        return self.sessions.register(session, activate=activate and not normalized_work_key)

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
        work_key: str = "",
        protected: bool = False,
    ) -> BrowserBridgeSession:
        normalized_work_key = normalize_work_key(work_key) if work_key else ""
        is_protected = bool(protected or normalized_work_key in PROTECTED_WORK_KEYS or looks_like_protected_label(label))
        endpoint_metadata = dict(metadata or {})
        if normalized_work_key:
            endpoint_metadata["work_key"] = normalized_work_key
            endpoint_metadata["protected"] = is_protected
        endpoint = BrowserEndpoint(
            kind=BrowserEndpointKind(endpoint_kind),
            url=endpoint_url,
            browser_name=browser_name or "chromium",
            metadata=endpoint_metadata,
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
            work_key=normalized_work_key,
            protected=is_protected,
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
        work_key: str = "",
        protected: bool = False,
        force_recreate: bool = False,
        queue_wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> BrowserBridgeSession:
        """Launch or reuse Chrome through PC Agent and register a local-agent bridge session."""
        from app.services.pc_agent_manager import pc_agent_manager

        normalized_work_key = normalize_work_key(work_key) if work_key else ""
        is_protected = bool(protected or normalized_work_key in PROTECTED_WORK_KEYS or looks_like_protected_label(label))
        launch_params: dict[str, Any] = {
            "url": url or "about:blank",
            "dynamic_port": True,
            "isolated_profile": isolated_profile,
            "new_window": False,
        }
        if preferred_port:
            launch_params["preferred_port"] = int(preferred_port)
        base_isolation_id = isolation_id or normalized_work_key
        if base_isolation_id:
            # Workflow sessions use one stable Chrome profile. Changing the
            # profile on recreate loses bank portal login/cookie state.
            launch_params["isolation_id"] = base_isolation_id
        if normalized_work_key:
            launch_params["work_key"] = normalized_work_key

        if self._route_pc_agent_via_active_api_first():
            active_queue_wait = (
                float(queue_wait_timeout_seconds)
                if queue_wait_timeout_seconds is not None
                else self._sidecar_queue_wait_timeout_seconds()
            )
            active_command_timeout = (
                float(command_timeout_seconds)
                if command_timeout_seconds is not None
                else self._sidecar_command_timeout_seconds("browser_launch")
            )
            active_routed = await self._execute_pc_agent_route_via_active_api(
                command_type="browser_launch",
                params=launch_params,
                agent_id=agent_id,
                job_type="browser_bridge_launch",
                required_capabilities=["interactive_browser"],
                queue_wait_timeout_seconds=active_queue_wait,
                lease_ttl_seconds=int(active_command_timeout + LOCAL_AGENT_LEASE_BUFFER_SECONDS),
                command_timeout_seconds=active_command_timeout,
            )
            routed = active_routed or {
                "status": "error",
                "error_code": "PC_AGENT_ROUTE_UNAVAILABLE",
                "message": "active AADS API PC Agent route unavailable",
            }
        else:
            direct_queue_wait = float(queue_wait_timeout_seconds) if queue_wait_timeout_seconds is not None else 120
            direct_command_timeout = float(command_timeout_seconds) if command_timeout_seconds is not None else 180
            routed = await pc_agent_manager.execute_routed_command(
                command_type="browser_launch",
                params=launch_params,
                agent_id=agent_id,
                job_type="browser_bridge_launch",
                required_capabilities=["interactive_browser"],
                queue_if_busy=True,
                wait_for_turn=True,
                queue_wait_timeout_seconds=direct_queue_wait,
                lease_ttl_seconds=int(direct_command_timeout + LOCAL_AGENT_LEASE_BUFFER_SECONDS),
                command_timeout_seconds=direct_command_timeout,
            )
            if routed.get("status") != "success" and str(routed.get("error_code") or "") in {"PC_AGENT_OFFLINE", "NO_CAPABLE_AGENT"}:
                active_routed = await self._execute_pc_agent_route_via_active_api(
                    command_type="browser_launch",
                    params=launch_params,
                    agent_id=agent_id,
                    job_type="browser_bridge_launch",
                    required_capabilities=["interactive_browser"],
                )
                if active_routed is not None:
                    routed = active_routed
        routed = self._coerce_pc_agent_embedded_success(routed)
        if routed.get("status") != "success":
            launch_error_code, _launch_error_message, _launch_error_detail = self._extract_pc_agent_route_error(routed)
            if launch_error_code in {"CDP_NOT_READY", "COMMAND_TIMEOUT"}:
                health_timeout = max(10.0, min(30.0, float(command_timeout_seconds or 30.0)))
                health_params: dict[str, Any] = {
                    "work_key": normalized_work_key or launch_params.get("work_key") or "",
                    "command_timeout_seconds": health_timeout,
                }
                if preferred_port:
                    health_params["preferred_port"] = int(preferred_port)
                health_routed = await self._execute_pc_agent_route_via_active_api(
                    command_type="browser_health",
                    params=health_params,
                    agent_id=agent_id,
                    job_type="browser_bridge_health_fallback",
                    required_capabilities=["interactive_browser"],
                    queue_wait_timeout_seconds=min(10.0, float(queue_wait_timeout_seconds or 10.0)),
                    lease_ttl_seconds=int(health_timeout + LOCAL_AGENT_LEASE_BUFFER_SECONDS),
                    command_timeout_seconds=health_timeout,
                )
                if health_routed is not None:
                    health_routed = self._coerce_pc_agent_embedded_success(health_routed)
                    if health_routed.get("status") == "success":
                        routed = health_routed
                if routed.get("status") != "success" and not self._route_pc_agent_via_active_api_first():
                    health_routed = await pc_agent_manager.execute_routed_command(
                        command_type="browser_health",
                        params=health_params,
                        agent_id=agent_id,
                        job_type="browser_bridge_health_fallback",
                        required_capabilities=["interactive_browser"],
                        queue_if_busy=True,
                        wait_for_turn=True,
                        queue_wait_timeout_seconds=min(10.0, float(queue_wait_timeout_seconds or 10.0)),
                        lease_ttl_seconds=int(health_timeout + LOCAL_AGENT_LEASE_BUFFER_SECONDS),
                        command_timeout_seconds=health_timeout,
                    )
                    health_routed = self._coerce_pc_agent_embedded_success(health_routed)
                    if health_routed.get("status") == "success":
                        routed = health_routed
                if routed.get("status") != "success":
                    tabs_params: dict[str, Any] = {
                        "work_key": normalized_work_key or launch_params.get("work_key") or "",
                        "command_timeout_seconds": health_timeout,
                    }
                    if preferred_port:
                        tabs_params["preferred_port"] = int(preferred_port)
                        tabs_params["port"] = int(preferred_port)
                    tabs_routed = await self._execute_pc_agent_route_via_active_api(
                        command_type="browser_tabs",
                        params=tabs_params,
                        agent_id=agent_id,
                        job_type="browser_bridge_tabs_fallback",
                        required_capabilities=["interactive_browser"],
                        queue_wait_timeout_seconds=min(10.0, float(queue_wait_timeout_seconds or 10.0)),
                        lease_ttl_seconds=int(health_timeout + LOCAL_AGENT_LEASE_BUFFER_SECONDS),
                        command_timeout_seconds=health_timeout,
                    )
                    tabs_routed = self._coerce_pc_agent_embedded_success(tabs_routed)
                    tabs_result = tabs_routed.get("result") if isinstance(tabs_routed, dict) else None
                    tabs_data = tabs_result.get("result") if isinstance(tabs_result, dict) else None
                    tabs = tabs_data.get("tabs") if isinstance(tabs_data, dict) else None
                    if tabs_routed.get("status") == "success" and isinstance(tabs, list):
                        routed = {
                            "status": "success",
                            "lease": tabs_routed.get("lease") or {"agent_id": agent_id},
                            "result": {
                                "result": {
                                    "port": int(preferred_port or tabs_data.get("port") or 9222),
                                    "work_key": normalized_work_key or launch_params.get("work_key") or "",
                                    "tabs_fallback": True,
                                }
                            },
                        }
        if routed.get("status") != "success":
            error_code, error_message, error_detail = self._extract_pc_agent_route_error(routed)
            raise BrowserBridgeError(
                error_message,
                error_code=error_code,
                detail=error_detail,
            )

        lease = routed.get("lease") or {}
        selected_agent_id = str(lease.get("agent_id") or agent_id or "")
        if not selected_agent_id:
            raise BrowserBridgeError("PC Agent browser_launch did not return agent_id")
        command_result = routed.get("result") or {}
        data = command_result.get("result") if isinstance(command_result, dict) else None
        if not isinstance(data, dict):
            raise BrowserBridgeError("PC Agent browser_launch returned no data")
        port = int(data.get("port") or preferred_port or 9222)
        existing = None if force_recreate else self.sessions.find_by_metadata(
            agent_id=selected_agent_id,
            port=str(port),
            endpoint_kind=BrowserEndpointKind.LOCAL_AGENT.value,
        )
        existing_is_protected = bool(
            existing and (existing.protected or existing.work_key in PROTECTED_WORK_KEYS or looks_like_protected_label(existing.label))
        )
        if existing and existing_is_protected and existing.work_key != normalized_work_key:
            existing = None
        if existing and normalized_work_key and existing.work_key and existing.work_key != normalized_work_key:
            existing = None
        metadata = {
            "agent_id": selected_agent_id,
            "port": str(port),
            "profile_dir": str(data.get("user_data_dir") or ""),
            "websocket_debugger_url": str(data.get("websocket_debugger_url") or ""),
            "endpoint_kind": BrowserEndpointKind.LOCAL_AGENT.value,
            "cdp_url": f"pc-agent://{selected_agent_id}/cdp/{port}",
            "last_url": url or "about:blank",
            "stale": False,
        }
        if normalized_work_key:
            metadata["work_key"] = normalized_work_key
            metadata["protected"] = is_protected
        if existing:
            existing.endpoint.metadata = metadata
            existing.label = label or existing.label
            if normalized_work_key:
                existing.work_key = normalized_work_key
                existing.protected = is_protected
                existing = self.sessions.bind_work_key(
                    existing,
                    work_key=normalized_work_key,
                    protected=is_protected,
                )
            else:
                self.sessions.touch(existing)
            if activate and not normalized_work_key:
                return self.select_session(existing.session_id)
            return existing
        return self.register_trusted_session(
            label=label,
            endpoint_kind=BrowserEndpointKind.LOCAL_AGENT.value,
            metadata=metadata,
            activate=activate and not normalized_work_key,
            created_by="pc_agent",
            work_key=normalized_work_key,
            protected=is_protected,
        )

    async def ensure_work_session(
        self,
        *,
        work_key: str,
        label: str = "",
        agent_id: str = "",
        url: str = "about:blank",
        preferred_port: int | None = None,
        force_recreate: bool = False,
        queue_wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> BrowserBridgeSession:
        """Return the dedicated Browser Bridge session for a business workflow.

        The active Browser Bridge session is intentionally left unchanged.
        Work sessions use isolated PC Agent Chrome profiles so login storage does
        not bleed into another workflow.
        """
        normalized_work_key = normalize_work_key(work_key)
        is_protected = normalized_work_key in PROTECTED_WORK_KEYS or looks_like_protected_label(label)
        existing = None if force_recreate else self.sessions.find_by_work_key(normalized_work_key)
        if existing and self._session_reusable(existing):
            existing.mark_used()
            if is_protected and not existing.protected:
                existing.protected = True
            self.sessions.touch(existing)
            logger.info(
                "browser_bridge_work_session_reused work_key=%s session_id=%s active_unchanged=true",
                normalized_work_key,
                existing.session_id,
            )
            return existing

        stale_existing = self.sessions.find_by_work_key(normalized_work_key) if force_recreate else existing
        if stale_existing:
            logger.warning(
                "browser_bridge_work_session_recreate work_key=%s old_session_id=%s reason=stale_or_expired",
                normalized_work_key,
                stale_existing.session_id,
            )
            self.sessions.retire_session(
                stale_existing.session_id,
                stale_reason="force_recreate" if force_recreate else "stale_or_expired",
                clear_work_key=True,
                clear_lease=True,
            )

        session = await self.ensure_pc_agent_cdp_session(
            agent_id=agent_id,
            label=label or default_work_session_label(normalized_work_key),
            url=url or "about:blank",
            preferred_port=preferred_port,
            isolated_profile=True,
            isolation_id=normalized_work_key,
            activate=False,
            work_key=normalized_work_key,
            protected=is_protected,
            force_recreate=force_recreate,
            queue_wait_timeout_seconds=queue_wait_timeout_seconds,
            command_timeout_seconds=command_timeout_seconds,
        )
        session.mark_used()
        self.sessions.touch(session)
        logger.info(
            "browser_bridge_work_session_ready work_key=%s session_id=%s active_unchanged=true protected=%s",
            normalized_work_key,
            session.session_id,
            is_protected,
        )
        return session

    def _session_reusable(self, session: BrowserBridgeSession) -> bool:
        if session.is_expired:
            return False
        if session.endpoint.kind == BrowserEndpointKind.LOCAL_AGENT:
            metadata = dict(session.endpoint.metadata or {})
            if metadata.get("stale"):
                return False
            if not (metadata.get("agent_id") and metadata.get("port")):
                return False
            return self._local_agent_online(session)
        browser = self._session_browsers.get(session.session_id)
        if browser is not None and hasattr(browser, "is_connected"):
            try:
                return bool(browser.is_connected())
            except Exception:
                return False
        return True

    @staticmethod
    def _local_agent_online(session: BrowserBridgeSession) -> bool:
        """Do not reuse a historical CDP record after its PC Agent disconnects."""
        try:
            from app.services.pc_agent_manager import pc_agent_manager

            metadata = dict(session.endpoint.metadata or {})
            agent_id = str(metadata.get("agent_id") or "").strip()
            return bool(agent_id and pc_agent_manager.get_agent(agent_id) is not None)
        except Exception:
            # Preserve availability if manager introspection itself is broken;
            # command routing will still perform the authoritative check.
            return True

    def _extract_pc_agent_route_error(self, result: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        detail = dict(result or {})
        command_result = detail.get("result") if isinstance(detail, dict) else None
        nested = command_result.get("result") if isinstance(command_result, dict) else None
        if nested is None and isinstance(detail.get("data"), dict):
            nested = detail.get("data")
        nested_detail = dict(nested or {}) if isinstance(nested, dict) else {}
        error_code = str(
            detail.get("error_code")
            or nested_detail.get("error_code")
            or ""
        ).strip()
        message = str(
            detail.get("message")
            or nested_detail.get("error")
            or nested_detail.get("message")
            or error_code
            or detail
        )
        combined_detail = dict(detail)
        if nested_detail:
            combined_detail.setdefault("command_result", nested_detail)
        return error_code, message, combined_detail

    @staticmethod
    def _coerce_pc_agent_embedded_success(result: dict[str, Any] | None) -> dict[str, Any]:
        """Accept late PC Agent successes wrapped by a route timeout response.

        route-execute can hit its HTTP/lease timeout just before the PC Agent
        posts the command result. In that case the wrapper status is error, but
        the embedded command result is already a valid success payload.
        """
        detail = dict(result or {})
        if str(detail.get("status") or "").lower() == "success":
            return detail
        command_result = detail.get("result") if isinstance(detail, dict) else None
        if not isinstance(command_result, dict):
            return detail
        if str(command_result.get("status") or "").lower() != "success":
            return detail
        coerced = dict(detail)
        coerced["status"] = "success"
        coerced["error_code"] = ""
        coerced["message"] = ""
        coerced["late_success_from_error_code"] = str(detail.get("error_code") or "")
        coerced["result"] = command_result
        return coerced

    def _mark_local_agent_session_healthy(
        self,
        session: BrowserBridgeSession,
        *,
        agent_id: str,
        port: int,
        last_url: str,
    ) -> None:
        metadata = dict(session.endpoint.metadata or {})
        metadata["agent_id"] = agent_id
        metadata["port"] = str(port)
        metadata["last_url"] = last_url or metadata.get("last_url") or "about:blank"
        metadata["stale"] = False
        metadata.pop("stale_reason", None)
        metadata["last_ok_at"] = utcnow().isoformat()
        session.endpoint.metadata = metadata
        session.mark_used()
        self.sessions.touch(session)

    @staticmethod
    def _pc_agent_offline_error_allows_online_retry(detail: dict[str, Any]) -> bool:
        """Allow active-peer retry only for generic offline routing failures.

        A configured default browser PC is a safety boundary. If the active API
        reports that the default browser PC is offline, retrying another online
        agent would open CEO/browser workflows on the wrong desktop.
        """
        message = str(detail.get("message") or "").strip().lower()
        if "default browser pc agent" in message:
            return False
        if detail.get("default_agent_id") or detail.get("default_hostname"):
            return False
        return True

    async def _recover_local_agent_session(
        self,
        session: BrowserBridgeSession,
        *,
        reason: str,
        agent_id: str,
        preferred_port: int,
        requested_url: str,
    ) -> BrowserBridgeSession | None:
        work_key = str(session.work_key or "").strip()
        if not work_key:
            return None
        self.sessions.retire_session(
            session.session_id,
            stale_reason=reason,
            clear_work_key=True,
            clear_lease=True,
        )
        self._session_contexts.pop(session.session_id, None)
        self._session_browsers.pop(session.session_id, None)
        logger.warning(
            "browser_bridge_local_agent_recover work_key=%s session_id=%s reason=%s",
            work_key,
            session.session_id,
            reason,
        )
        return await self.ensure_work_session(
            work_key=work_key,
            label=session.label,
            agent_id=agent_id,
            url=requested_url or str((session.endpoint.metadata or {}).get("last_url") or "about:blank"),
            preferred_port=preferred_port or None,
            force_recreate=True,
        )

    def work_session_status(self) -> dict[str, Any]:
        self.sessions.prune_stale_sessions()
        active = self.active_session()
        return {
            "active_session": active.session_id if active else None,
            "work_sessions": self.sessions.public_work_sessions(),
            "sessions": list(self.sessions.public_sessions()),
        }

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
        cached_url = str(getattr(self, "_active_api_route_url_cache", "") or "")
        if cached_url in deduped_urls:
            deduped_urls = [cached_url, *[url for url in deduped_urls if url != cached_url]]

        def _post() -> dict[str, Any] | None:
            request_timeout_seconds = max(
                10.0,
                min(300.0, float(queue_wait_timeout_seconds) + float(command_timeout_seconds) + 5.0),
            )
            for url in deduped_urls:
                body = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=request_timeout_seconds) as resp:
                        raw = resp.read().decode("utf-8")
                except urllib.error.HTTPError as exc:
                    try:
                        raw_error = exc.read().decode("utf-8", errors="ignore")
                        parsed_error = json.loads(raw_error)
                    except Exception:
                        logger.warning("browser_bridge_active_pc_agent_fallback_http_failed url=%s err=%s", url, exc)
                        continue
                    detail = parsed_error.get("detail") if isinstance(parsed_error, dict) else None
                    if isinstance(detail, dict):
                        error_code = str(detail.get("error_code") or "")
                        if error_code in {"PC_AGENT_OFFLINE", "NO_CAPABLE_AGENT"}:
                            fallback_agent_id = ""
                            if (
                                not agent_id
                                and error_code == "PC_AGENT_OFFLINE"
                                and self._pc_agent_offline_error_allows_online_retry(detail)
                            ):
                                fallback_agent_id = self._active_api_online_agent_id_for_route_url(
                                    url,
                                    required_capabilities,
                                    request_timeout_seconds,
                                )
                            if fallback_agent_id:
                                retry_payload = dict(payload)
                                retry_payload["agent_id"] = fallback_agent_id
                                retry_req = urllib.request.Request(
                                    url,
                                    data=json.dumps(retry_payload).encode("utf-8"),
                                    headers={"Content-Type": "application/json"},
                                    method="POST",
                                )
                                try:
                                    with urllib.request.urlopen(retry_req, timeout=request_timeout_seconds) as resp:
                                        retry_raw = resp.read().decode("utf-8")
                                    self._active_api_route_url_cache = url
                                    return json.loads(retry_raw)
                                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as retry_exc:
                                    logger.warning(
                                        "browser_bridge_active_pc_agent_fallback_online_agent_retry_failed "
                                        "url=%s agent_id=%s err=%s",
                                        url,
                                        fallback_agent_id,
                                        retry_exc,
                                    )
                            logger.warning(
                                "browser_bridge_active_pc_agent_fallback_route_unavailable url=%s error_code=%s",
                                url,
                                error_code,
                            )
                            continue
                        return self._coerce_pc_agent_embedded_success(detail)
                    logger.warning("browser_bridge_active_pc_agent_fallback_http_bad_detail url=%s err=%s", url, exc)
                    continue
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    logger.warning("browser_bridge_active_pc_agent_fallback_failed url=%s err=%s", url, exc)
                    continue
                try:
                    parsed = self._coerce_pc_agent_embedded_success(json.loads(raw))
                    self._active_api_route_url_cache = url
                    return parsed
                except json.JSONDecodeError as exc:
                    logger.warning("browser_bridge_active_pc_agent_fallback_bad_json url=%s err=%s", url, exc)
                    continue
            return None

        return await asyncio.to_thread(_post)

    @staticmethod
    def _route_pc_agent_via_active_api_first() -> bool:
        flag = str(os.getenv("AADS_PC_AGENT_ROUTE_ACTIVE_API_FIRST") or "").strip().lower()
        if flag in {"1", "true", "yes", "on"}:
            return True
        if flag in {"0", "false", "no", "off"}:
            return False
        service_role = str(os.getenv("AADS_SERVICE_ROLE") or "").strip().lower()
        return service_role in SIDECAR_SERVICE_ROLES

    @staticmethod
    def _sidecar_queue_wait_timeout_seconds() -> float:
        try:
            return max(1.0, float(os.getenv("AADS_PC_AGENT_SIDECAR_QUEUE_WAIT_SECONDS", SIDECAR_QUEUE_WAIT_SECONDS)))
        except ValueError:
            return float(SIDECAR_QUEUE_WAIT_SECONDS)

    @staticmethod
    def _sidecar_command_timeout_seconds(command_type: str) -> float:
        env_map = {
            "browser_launch": ("AADS_PC_AGENT_SIDECAR_LAUNCH_TIMEOUT_SECONDS", SIDECAR_LAUNCH_TIMEOUT_SECONDS),
            "browser_navigate": ("AADS_PC_AGENT_SIDECAR_NAVIGATION_TIMEOUT_SECONDS", SIDECAR_NAVIGATION_TIMEOUT_SECONDS),
            "browser_download": ("AADS_PC_AGENT_SIDECAR_NAVIGATION_TIMEOUT_SECONDS", SIDECAR_NAVIGATION_TIMEOUT_SECONDS),
        }
        if command_type in LOCAL_AGENT_JS_COMMANDS or command_type in {"browser_screenshot", "browser_tabs"}:
            env_name, default = "AADS_PC_AGENT_SIDECAR_SNAPSHOT_TIMEOUT_SECONDS", SIDECAR_SNAPSHOT_TIMEOUT_SECONDS
        else:
            env_name, default = env_map.get(
                command_type,
                ("AADS_PC_AGENT_SIDECAR_COMMAND_TIMEOUT_SECONDS", SIDECAR_COMMAND_TIMEOUT_SECONDS),
            )
        try:
            return max(1.0, float(os.getenv(env_name, default)))
        except ValueError:
            return float(default)

    @classmethod
    def _active_api_online_agent_id_for_route_url(
        cls,
        route_url: str,
        required_capabilities: list[str],
        timeout_seconds: float,
    ) -> str:
        """Find an online browser-capable PC Agent from the active API status endpoint.

        Sidecar services do not own the websocket registry. If the active AADS API
        has an offline pinned default browser agent, route-execute can reject an
        otherwise valid browser request. In that case, retry against an explicitly
        online interactive browser agent exposed by the same API instance.
        """
        status_url = str(route_url).replace("/route-execute", "/status")
        required = {str(cap or "").strip().lower() for cap in required_capabilities if str(cap or "").strip()}
        try:
            with urllib.request.urlopen(status_url, timeout=max(5.0, min(15.0, timeout_seconds))) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.warning("browser_bridge_active_pc_agent_status_failed url=%s err=%s", status_url, exc)
            return ""

        agents = data.get("agents") if isinstance(data, dict) else []
        if not isinstance(agents, list):
            return ""
        candidates: list[dict[str, Any]] = []
        for item in agents:
            if not isinstance(item, dict) or str(item.get("status") or "").lower() != "online":
                continue
            capabilities = {str(cap or "").strip().lower() for cap in (item.get("capabilities") or [])}
            if required and not required.issubset(capabilities):
                continue
            agent_id = str(item.get("agent_id") or "").strip()
            if agent_id:
                candidates.append(item)
        candidates.sort(key=lambda item: float(item.get("heartbeat_age_seconds") or 999999))
        return str(candidates[0].get("agent_id") or "").strip() if candidates else ""

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
        urls: list[str] = []
        active_container = cls._active_container_name()
        if active_container:
            urls.append(f"http://{active_container}:8080/api/v1/pc-agent/route-execute")
        # In sidecar containers such as yeoljeong-finance-worker, loopback points
        # back to the sidecar itself. Try Docker service DNS before loopback.
        if active_port == "8100":
            urls.append("http://aads-server:8080/api/v1/pc-agent/route-execute")
        elif active_port == "8102":
            urls.append("http://aads-server-green:8080/api/v1/pc-agent/route-execute")
        urls.append("http://aads-server:8080/api/v1/pc-agent/route-execute")
        urls.append("http://aads-server-green:8080/api/v1/pc-agent/route-execute")
        urls.append("http://127.0.0.1:8080/api/v1/pc-agent/route-execute")
        urls.append(f"http://127.0.0.1:{active_port}/api/v1/pc-agent/route-execute")
        docker_hosts = ["host.docker.internal", *cls._docker_default_gateway_hosts(), "172.17.0.1"]
        urls.extend(f"http://{host}:{active_port}/api/v1/pc-agent/route-execute" for host in docker_hosts)
        deduped: list[str] = []
        for url in urls:
            if url not in deduped:
                deduped.append(url)
        return deduped

    @staticmethod
    def _docker_default_gateway_hosts() -> list[str]:
        hosts: list[str] = []
        env_host = os.getenv("AADS_DOCKER_HOST_GATEWAY", "").strip()
        if env_host:
            hosts.append(env_host)
        try:
            with open("/proc/net/route", "r", encoding="utf-8") as handle:
                for line in handle.readlines()[1:]:
                    fields = line.split()
                    if len(fields) < 3 or fields[1] != "00000000":
                        continue
                    gateway_hex = fields[2]
                    gateway = ".".join(str(byte) for byte in bytes.fromhex(gateway_hex)[::-1])
                    if gateway and gateway != "0.0.0.0":
                        hosts.append(gateway)
                    break
        except (OSError, ValueError):
            pass
        deduped: list[str] = []
        for host in hosts:
            if host not in deduped:
                deduped.append(host)
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

    @staticmethod
    def _resolve_playwright_browsers_path() -> str | None:
        """설치된 Playwright 브라우저 번들 경로를 탐색해 환경변수로 고정한다.

        Playwright는 업그레이드마다 chromium 빌드번호가 바뀌므로 디렉터리명을
        하드코딩하면 안 된다(과거 chromium-1208 하드코딩 → 1234 설치 시 미탐지).
        또한 HOME이 /root가 아닌 워커(예: 채팅 세션 서브프로세스)에서는 기본
        탐색 경로($HOME/.cache/ms-playwright)가 비어 있어 실행 파일을 못 찾는다.
        """
        existing = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if existing and os.path.isdir(existing):
            return existing

        candidates = [
            "/root/.cache/ms-playwright",
            os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright"),
            "/ms-playwright",
            "/root/.cache",
        ]
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if not os.path.isdir(candidate):
                continue
            try:
                entries = os.listdir(candidate)
            except OSError:
                continue
            for entry in entries:
                if not entry.startswith(("chromium-", "chromium_headless_shell-")):
                    continue
                if os.path.isdir(os.path.join(candidate, entry)):
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = candidate
                    logger.info(
                        "playwright_browsers_path_resolved path=%s bundle=%s",
                        candidate,
                        entry,
                    )
                    return candidate
        logger.warning(
            "playwright_browsers_path_not_found candidates=%s", candidates
        )
        return None

    async def _ensure_playwright(self) -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserBridgeError("playwright 패키지가 설치되지 않았습니다") from exc

        if self._pw_handle is None:
            # 반드시 드라이버 기동 전에 설정해야 한다. Node 드라이버 프로세스가
            # spawn 시점에 PLAYWRIGHT_BROWSERS_PATH를 상속받기 때문이다.
            self._resolve_playwright_browsers_path()
            self._pw_handle = await async_playwright().start()
        return self._pw_handle

    async def _headless_fallback_context(self) -> Any:
        pw = await self._ensure_playwright()

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
