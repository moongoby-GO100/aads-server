"""Opt-in, fail-closed reclamation of tabs created by this agent.

The in-memory ledger deliberately does not survive an agent restart: a lost
creation proof must never be reconstructed from a CDP /json listing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import browser_auto as cdp

try:
    import psutil
except ImportError:  # no process/port proof means no ownership
    psutil = None

_OWNED: dict[str, OwnedTab] = {}
_USED: set[str] = set()
_OWNED_LOCK = threading.Lock()
_MAX_PER_PASS = 2
_MAX_OWNED = 64
_SWEEP_CURSOR = 0
_SWEEP_WINDOW = 0
_SWEEP_COUNT = 0
_CREATE_ATTEMPTS = 0
_QUARANTINED: dict[tuple[int, str], str] = {}
logger = logging.getLogger(__name__)
_ACTIVITY_JS = """(() => ({
  visible: document.visibilityState === 'visible',
  focused: document.hasFocus(),
  editing: !!document.querySelector('input:focus,textarea:focus,[contenteditable]:focus') ||
    [...document.querySelectorAll('input,textarea,[contenteditable]')].some(e =>
      !['hidden','submit','button','checkbox','radio'].includes(e.type) &&
      !!(e.value || e.textContent || '').trim()),
  downloading: !!document.querySelector('a[download]:focus'),
  url: location.href
}))()"""


@dataclass(frozen=True)
class OwnedTab:
    token: str
    epoch: str
    work_key: str
    port: int
    pid: int
    process_started: float
    browser_ws: str
    target_id: str
    target_ws: str
    lease_until: float
    created_at: float
    session_generation: str = ""


def mark_observed(target_id: str) -> None:
    """An interactive binding permanently excludes this target from sweeping."""
    with _OWNED_LOCK:
        if any(record.target_id == target_id for record in _OWNED.values()):
            _USED.add(target_id)


def _forget(record: OwnedTab) -> None:
    global _CREATE_ATTEMPTS
    with _OWNED_LOCK:
        if _OWNED.get(record.token) is record:
            _OWNED.pop(record.token)
            _CREATE_ATTEMPTS = max(0, _CREATE_ATTEMPTS - 1)
            if not any(item.target_id == record.target_id for item in _OWNED.values()):
                _USED.discard(record.target_id)


async def _prune_closed_or_changed(port: int) -> None:
    """Discard only records whose loss is confirmed by this browser's CDP inventory.

    The caller holds the port lock. An uncertain or malformed response preserves
    the ledger, and the hard cap then rejects further creations.
    """
    global _CREATE_ATTEMPTS
    records = [record for record in _OWNED.values() if record.port == port]
    quarantined = [key for key in _QUARANTINED if key[0] == port]
    if not records and not quarantined:
        return
    version = await cdp._probe_cdp_version(port)
    browser_ws = str((version or {}).get("webSocketDebuggerUrl") or "")
    if not _local_ws(browser_ws, port):
        return
    try:
        response = await cdp._send_cdp(browser_ws, "Target.getTargets", {}, timeout_seconds=5)
    except Exception:  # noqa: BLE001 - uncertain inventory must not discard proof
        return
    targets = response.get("targetInfos") if isinstance(response, dict) else None
    if not isinstance(targets, list) or not all(isinstance(t, dict) and isinstance(t.get("targetId"), str) for t in targets):
        return
    try:
        pages = await cdp._list_cdp_targets(port)
    except Exception:  # noqa: BLE001 - a failed listing is not proof of change
        return
    if not isinstance(pages, list) or not all(isinstance(page, dict) for page in pages):
        return
    by_id = {target["targetId"]: target for target in targets}
    pages_by_id = {page.get("id"): page for page in pages}
    for key in quarantined:
        if key[1] not in by_id and key[1] not in pages_by_id:
            with _OWNED_LOCK:
                if key in _QUARANTINED:
                    _QUARANTINED.pop(key)
                    _CREATE_ATTEMPTS = max(0, _CREATE_ATTEMPTS - 1)
    for record in records:
        if (_OWNED.get(record.token) is record and record.browser_ws == browser_ws
                and _process_proof(record.pid, port) == record.process_started):
            target = by_id.get(record.target_id)
            page = pages_by_id.get(record.target_id)
            if ((target is None and page is None)
                    or (target is not None and target.get("type") != "page")
                    or (target is not None and page is not None
                        and page.get("webSocketDebuggerUrl") != record.target_ws)):
                _forget(record)


def _process_proof(pid: int, port: int) -> float | None:
    """Verify the process behind the listening port and return its start time."""
    if psutil is None or pid <= 0:
        return None
    try:
        proc = psutil.Process(pid)
        # psutil < 6 uses connections(); both APIs expose the same listener data.
        connection_list = getattr(proc, "net_connections", None) or proc.connections
        listeners = connection_list(kind="tcp")
        if not any(
            conn.status == psutil.CONN_LISTEN
            and conn.laddr.port == port
            and conn.laddr.ip in {"127.0.0.1", "::1"}
            for conn in listeners
        ):
            return None
        return proc.create_time()
    except (psutil.Error, OSError, AttributeError):
        return None


def _local_ws(url: str, port: int) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "ws" and parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port == port


async def _target_info(browser_ws: str, target_id: str) -> dict[str, Any] | None:
    try:
        response = await cdp._send_cdp(browser_ws, "Target.getTargetInfo", {"targetId": target_id}, timeout_seconds=5)
        info = response.get("targetInfo") if isinstance(response, dict) else None
        return info if isinstance(info, dict) and info.get("targetId") == target_id else None
    except Exception:  # noqa: BLE001 - an uncertain CDP result must block reclamation
        return None


async def _activity(target_ws: str) -> dict[str, Any] | None:
    try:
        response = await cdp._send_cdp(
            target_ws, "Runtime.evaluate",
            {"expression": _ACTIVITY_JS, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=5,
        )
        result = response.get("result") if isinstance(response, dict) else None
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(result, dict) or result.get("exceptionDetails") or not isinstance(value, dict):
            return None
        if not all(type(value.get(key)) is bool for key in ("visible", "focused", "editing", "downloading")):
            return None
        return value if isinstance(value.get("url"), str) else None
    except Exception:  # noqa: BLE001 - an uncertain activity result must block reclamation
        return None


def _activity_value(response: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(response, dict) or response.get("exceptionDetails"):
        return None
    result = response.get("result") if isinstance(response, dict) else None
    value = result.get("value") if isinstance(result, dict) else None
    if (not isinstance(value, dict) or not all(type(value.get(k)) is bool
            for k in ("visible", "focused", "editing", "downloading"))
            or not isinstance(value.get("url"), str)):
        return None
    return value


def _ownership_current(record: OwnedTab) -> bool:
    session = cdp.CDPSessionManager.get_session(record.work_key)
    return bool(
        _OWNED.get(record.token) is record and record.token and record.epoch
        and session and record.session_generation
        and session.generation == record.session_generation
        and session.port == record.port and session.pid == record.pid
        and time.monotonic() >= record.lease_until
        and record.target_id not in _USED
        and session.last_target_id != record.target_id
        and session.last_heartbeat_at <= time.time() - 60
        and cdp._is_managed_profile_dir(session.profile_dir)
        and _process_proof(record.pid, record.port) == record.process_started
    )


async def _guarded_close(record: OwnedTab) -> str:
    """Observe the target and close it over one attached browser connection.

    CDP navigation/target events received before the close acknowledgement veto
    confirmation. A separate client can still race the browser after the command
    is sent; therefore this path is limited to a proven untouched blank tab.
    """
    import websockets

    if not _ownership_current(record):
        return "ownership_or_session_changed"
    ws = await cdp._connect_cdp_ws(websockets, record.browser_ws, open_timeout=5)
    session_id = ""
    changed = False

    async def request(method: str, params: dict[str, Any] | None = None, *, target_session: bool = False) -> dict[str, Any]:
        nonlocal changed
        msg_id = cdp._next_id()
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if target_session:
            payload["sessionId"] = session_id
        await ws.send(json.dumps(payload))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            event = message.get("method", "")
            if event in {"Page.frameStartedNavigating", "Page.frameNavigated", "Page.navigatedWithinDocument",
                         "Runtime.executionContextsCleared", "Target.detachedFromTarget", "Target.targetDestroyed"}:
                changed = True
            if event == "Target.targetInfoChanged":
                info = (message.get("params") or {}).get("targetInfo") or {}
                if info.get("targetId") == record.target_id and info.get("url") != "about:blank":
                    changed = True
            if message.get("id") == msg_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}

    try:
        attached = await request("Target.attachToTarget", {"targetId": record.target_id, "flatten": True})
        session_id = str(attached.get("sessionId") or "")
        if not session_id:
            return "attach_failed"
        await request("Page.enable", target_session=True)
        await request("Runtime.enable", target_session=True)
        changed = False  # discard only setup events, before the final activity sample
        response = await request("Runtime.evaluate", {"expression": _ACTIVITY_JS, "returnByValue": True,
                                                       "awaitPromise": False}, target_session=True)
        activity = _activity_value(response)
        if changed or activity is None or any(activity[k] for k in ("visible", "focused", "editing", "downloading")):
            return "user_active_or_changed"
        if activity["url"] != "about:blank":
            return "non_blank_protected"
        info = await request("Target.getTargetInfo", {"targetId": record.target_id})
        target = info.get("targetInfo") or {}
        if changed or target.get("targetId") != record.target_id or target.get("type") != "page" or target.get("url") != "about:blank":
            return "target_changed"
        if not _ownership_current(record):
            return "ownership_or_session_changed"
        closed = await request("Target.closeTarget", {"targetId": record.target_id})
        if changed or not _ownership_current(record):
            return "changed_during_close"
        return "closed" if closed.get("success") is True else "close_not_confirmed"
    finally:
        if session_id:
            try:
                await request("Target.detachFromTarget", {"sessionId": session_id})
            except Exception as exc:  # noqa: BLE001 - tab may already have closed
                logger.debug("target detach failed after close check: %s", exc)
        await ws.close()


async def create(params: dict[str, Any]) -> dict[str, Any]:
    """Create a new target and record ownership only after independent checks."""
    global _CREATE_ATTEMPTS
    try:
        lease = max(60, min(int(params.get("lease_seconds") or 300), 3600))
    except (TypeError, ValueError, OverflowError):
        return {"status": "error", "data": {"reason": "invalid_lease"}}
    work_key = str(params.get("work_key") or "")
    session = cdp.CDPSessionManager.get_session(work_key)
    if not session or session.pid <= 0 or not cdp._is_managed_profile_dir(session.profile_dir):
        return {"status": "error", "data": {"reason": "managed_session_required"}}
    port = session.port
    async with cdp._owned_tab_lock(port):
        await _prune_closed_or_changed(port)
        if _CREATE_ATTEMPTS >= _MAX_OWNED:
            return {"status": "error", "data": {"reason": "creation_attempt_capacity_reached"}}
        if len(_OWNED) >= _MAX_OWNED:
            return {"status": "error", "data": {"reason": "ownership_capacity_reached"}}
        started = _process_proof(session.pid, port)
        version = await cdp._probe_cdp_version(port)
        browser_ws = str((version or {}).get("webSocketDebuggerUrl") or "")
        if started is None or not _local_ws(browser_ws, port):
            return {"status": "error", "data": {"reason": "process_port_unverified"}}
        # Create through the verified browser endpoint. Never adopt an existing tab.
        target_id = ""
        try:
            # A foreground target remains visible in headful Chrome and cannot
            # pass the activity gate. Background creation keeps it eligible.
            _CREATE_ATTEMPTS += 1
            created = await cdp._send_cdp(browser_ws, "Target.createTarget", {"url": "about:blank", "background": True}, timeout_seconds=5)
            target_id = str(created.get("targetId") or "")
            pages = await cdp._list_cdp_targets(port)
            page = next((p for p in pages if p.get("id") == target_id and p.get("type") == "page"), None)
            info = await _target_info(browser_ws, target_id) if page else None
            target_ws = str((page or {}).get("webSocketDebuggerUrl") or "")
            if (not target_id or not info or info.get("type") != "page"
                    or info.get("attached") is not False or page.get("url") != "about:blank"
                    or not _local_ws(target_ws, port)
                    or _process_proof(session.pid, port) != started
                    or cdp.CDPSessionManager.get_session(work_key) is not session):
                raise ValueError("creation_proof_failed")
            now = time.monotonic()
            token = uuid.uuid4().hex
            with _OWNED_LOCK:
                if len(_OWNED) >= _MAX_OWNED:
                    raise ValueError("ownership_capacity_reached")
                _OWNED[token] = OwnedTab(token, uuid.uuid4().hex, work_key, port, session.pid,
                                         started, browser_ws, target_id, target_ws, now + lease, now,
                                         session.generation)
            return {"status": "success", "data": {"owner_token": token, "target_id": target_id, "lease_seconds": lease}}
        except Exception as exc:  # noqa: BLE001 - creation proof must fail closed
            reason = "creation_proof_failed" if isinstance(exc, ValueError) else "creation_failed"
            cleanup = "not_created"
            if target_id:
                cleanup = "quarantined"
                _QUARANTINED[(port, target_id)] = reason
                logger.warning("owned tab quarantined after failed creation proof: target=%s reason=%s", target_id, reason)
            return {"status": "error", "data": {"reason": reason, "target_id": target_id, "cleanup": cleanup}}


async def reclaim(params: dict[str, Any]) -> dict[str, Any]:
    """One bounded pass; disabled and dry-run unless explicitly enabled locally."""
    global _SWEEP_CURSOR, _SWEEP_WINDOW, _SWEEP_COUNT
    enabled = os.getenv("AADS_SAFE_TAB_RECLAIM_ENABLED") == "1"
    dry_run = params.get("dry_run") is not False
    selected = str(params.get("owner_token") or "")
    results: list[dict[str, Any]] = []
    # Hold the same per-port lock used by command dispatch and tab binding from
    # final observation through close. The pass cap applies across all ports.
    # Selection itself is local and has no awaits. The cap applies to every caller.
    candidates = [r for r in _OWNED.values() if not selected or r.token == selected]
    if candidates and not selected:
        start = _SWEEP_CURSOR % len(candidates)
        candidates = candidates[start:] + candidates[:start]
        _SWEEP_CURSOR += _MAX_PER_PASS
    window = int(time.monotonic() // 60)
    if window != _SWEEP_WINDOW:
        _SWEEP_WINDOW, _SWEEP_COUNT = window, 0
    entries = candidates[:max(0, _MAX_PER_PASS - _SWEEP_COUNT)]
    _SWEEP_COUNT += len(entries)
    for record in entries:
        async with cdp._owned_tab_lock(record.port):
            try:
                reason = await _reclaim_reason(record)
            except Exception:  # noqa: BLE001 - one uncertain record must not abort the pass
                reason = "ownership_unknown"
            if reason == "eligible" and enabled and not dry_run:
                # All checks, including the last CDP lookup and close, run under this port lock.
                try:
                    close_reason = await _guarded_close(record)
                    if close_reason == "closed":
                        _forget(record)
                        reason = "closed"
                    else:
                        reason = close_reason
                except Exception:  # noqa: BLE001 - preserve the ownership record
                    reason = "close_failed"
            elif reason == "eligible":
                reason = "dry_run" if enabled else "disabled"
            elif reason == "tab_changed":
                await _prune_closed_or_changed(record.port)
                if _OWNED.get(record.token) is not record:
                    reason = "ownership_lost"
            results.append({"owner_token": record.token, "target_id": record.target_id, "result": reason})
    return {"status": "success", "data": {"results": results, "processed": len(results), "limit": _MAX_PER_PASS}}


async def _reclaim_reason(record: OwnedTab) -> str:
    if _OWNED.get(record.token) is not record or time.monotonic() < record.lease_until:
        return "lease_active_or_changed"
    if record.target_id in _USED:
        return "user_observed"
    session = cdp.CDPSessionManager.get_session(record.work_key)
    if (not session or session.port != record.port or session.pid != record.pid
            or not record.session_generation or session.generation != record.session_generation
            or getattr(session, "last_target_id", None) is None
            or session.last_target_id == record.target_id
            or getattr(session, "last_heartbeat_at", None) is None
            or session.last_heartbeat_at > time.time() - 60
            or not cdp._is_managed_profile_dir(session.profile_dir)):
        return "session_or_command_active"
    if _process_proof(record.pid, record.port) != record.process_started:
        return "process_changed"
    version = await cdp._probe_cdp_version(record.port)
    if str((version or {}).get("webSocketDebuggerUrl") or "") != record.browser_ws:
        return "browser_changed"
    pages = await cdp._list_cdp_targets(record.port)
    page = next((p for p in pages if p.get("id") == record.target_id and p.get("type") == "page"), None)
    if not page or page.get("webSocketDebuggerUrl") != record.target_ws:
        return "tab_changed"
    info = await _target_info(record.browser_ws, record.target_id)
    if not info or info.get("attached") is not False or info.get("type") != "page":
        return "attached_or_unknown"
    # Existing live-control bindings are observers even if CDP reports detached.
    from . import browser_tab
    if any(tab.target_id == record.target_id for tab in browser_tab._SESSIONS.values()):
        return "observed"
    activity = await _activity(record.target_ws)
    if activity is None or any(activity[key] for key in ("visible", "focused", "editing", "downloading")):
        return "user_active_or_unknown"
    # CDP has no reliable snapshot of in-flight downloads or unsaved app state.
    # A blank target created here is the only target whose state we can prove.
    if activity["url"] != "about:blank" or page.get("url") != "about:blank":
        return "non_blank_protected"
    return "eligible"


async def execute(params: dict[str, Any]) -> dict[str, Any]:
    operation = params.get("op")
    if operation == "create":
        return await create(params)
    if operation == "reclaim":
        return await reclaim(params)
    return {"status": "error", "data": {"reason": "unsupported_operation"}}
