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
                merged.setdefault("evaluate_timeout_seconds", max(1.0, min(20.0, command_timeout_seconds - 0.5)))

            lease_ttl_seconds = int(command_timeout_seconds + LOCAL_AGENT_LEASE_BUFFER_SECONDS)
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
            if result.get("status") != "success" and str(result.get("error_code") or "") == "PC_AGENT_OFFLINE":
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
            ):
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

    async def goto(self, url: str, **_: Any) -> None:
        await self._run_browser_command(
            "browser_navigate",
            {"url": url},
            command_timeout_seconds=LOCAL_AGENT_NAVIGATION_TIMEOUT_SECONDS,
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

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        expr = expression.strip()
        if arg is not None:
            # Keep sensitive arguments beyond the PC Agent's 200-character log preview.
            redacted_prefix = "/* browser-bridge argument redacted */" + (" " * 220)
            expr = f"{redacted_prefix}({expr})({json.dumps(arg)})"
        # Wrap no-argument arrow/function expressions as an IIFE to match
        # Playwright page.evaluate() semantics.
        elif self._FUNC_EXPR_RE.match(expr):
            expr = f"({expr})()"
        data = await self._run_browser_command(
            "browser_eval",
            {"expression": expr},
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
    ) -> BrowserBridgeSession:
        """Launch or reuse Chrome through PC Agent and register a local-agent bridge session."""
        from app.services.pc_agent_manager import pc_agent_manager

        normalized_work_key = normalize_work_key(work_key) if work_key else ""
        is_protected = bool(protected or normalized_work_key in PROTECTED_WORK_KEYS or looks_like_protected_label(label))
        launch_params: dict[str, Any] = {
            "url": url or "about:blank",
            "dynamic_port": True,
            "isolated_profile": isolated_profile,
            "new_window": True,
        }
        if preferred_port:
            launch_params["preferred_port"] = int(preferred_port)
        if isolation_id or normalized_work_key:
            launch_params["isolation_id"] = isolation_id or normalized_work_key
        if normalized_work_key:
            launch_params["work_key"] = normalized_work_key

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

        def _post() -> dict[str, Any] | None:
            body = json.dumps(payload).encode("utf-8")
            request_timeout_seconds = max(
                10.0,
                min(300.0, float(queue_wait_timeout_seconds) + float(command_timeout_seconds) + 5.0),
            )
            for url in deduped_urls:
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
                            logger.warning(
                                "browser_bridge_active_pc_agent_fallback_route_unavailable url=%s error_code=%s",
                                url,
                                error_code,
                            )
                            continue
                        return detail
                    logger.warning("browser_bridge_active_pc_agent_fallback_http_bad_detail url=%s err=%s", url, exc)
                    continue
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
