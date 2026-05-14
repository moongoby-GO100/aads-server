"""AADS: CDP 브라우저 자동화 — Chrome DevTools Protocol via WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import urllib.request
import uuid
from dataclasses import dataclass, field
from time import time as _time
from typing import Any, Dict

logger = logging.getLogger(__name__)

CDP_HOST = "localhost"
CDP_PORT = 9222
CDP_CONNECT_TIMEOUT_SECONDS = float(os.getenv("AADS_CDP_CONNECT_TIMEOUT_SECONDS", "5") or "5")
CDP_COMMAND_TIMEOUT_SECONDS = float(os.getenv("AADS_CDP_COMMAND_TIMEOUT_SECONDS", "15") or "15")
CDP_EVALUATE_TIMEOUT_SECONDS = float(os.getenv("AADS_CDP_EVALUATE_TIMEOUT_SECONDS", "12") or "12")
CDP_RECOVERY_RETRY_LIMIT = max(0, int(os.getenv("AADS_CDP_RECOVERY_RETRY_LIMIT", "1") or "1"))
VVIC_SPA_MIN_TEXT_LENGTH = max(40, int(os.getenv("AADS_VVIC_SPA_MIN_TEXT_LENGTH", "120") or "120"))
_ERROR_CDP_NOT_READY = "CDP_NOT_READY"
_ERROR_RUNTIME_EVALUATE_TIMEOUT = "RUNTIME_EVALUATE_TIMEOUT"
_ERROR_STALE_TARGET = "STALE_TARGET"
_ERROR_SYNTAX_ERROR = "SYNTAX_ERROR"
_ERROR_SPA_SHELL_ONLY = "SPA_SHELL_ONLY"
_MSG_ID = 0


class CDPCommandError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code or _ERROR_CDP_NOT_READY)
        self.details = dict(details or {})


@dataclass
class CDPSession:
    work_key: str
    port: int
    profile_dir: str
    pid: int = 0
    connected_at: float = field(default_factory=_time)
    last_heartbeat_at: float = field(default_factory=_time)
    last_target_id: str = ""
    last_target_url: str = ""
    last_error_code: str = ""


class CDPSessionManager:
    _sessions: dict[str, CDPSession] = {}
    _port_pool: list[int] = [9222, 9333, 9444, 9555, 9666, 9777]
    _lock = threading.Lock()

    @classmethod
    def normalize_work_key(cls, work_key: str) -> str:
        return str(work_key or "general").strip() or "general"

    @classmethod
    def get_session(cls, work_key: str) -> CDPSession | None:
        return cls._sessions.get(cls.normalize_work_key(work_key))

    @classmethod
    def get_by_port(cls, port: int) -> CDPSession | None:
        with cls._lock:
            for session in cls._sessions.values():
                if session.port == port:
                    return session
        return None

    @classmethod
    def allocate_port(
        cls,
        work_key: str,
        preferred: int | None = None,
        candidates: list[int] | None = None,
    ) -> int:
        normalized_work_key = cls.normalize_work_key(work_key)
        with cls._lock:
            existing = cls._sessions.get(normalized_work_key)
            if existing:
                return existing.port
            used = {s.port for s in cls._sessions.values()}
            ordered: list[int] = []
            if preferred:
                ordered.append(preferred)
            ordered.extend(candidates or cls._port_pool)
            ordered.extend(cls._port_pool)
            for port in ordered:
                if port not in used:
                    return port
        return _find_free_port()

    @classmethod
    def register(cls, work_key: str, port: int, profile_dir: str, pid: int = 0) -> CDPSession:
        normalized_work_key = cls.normalize_work_key(work_key)
        with cls._lock:
            session = CDPSession(work_key=normalized_work_key, port=port, profile_dir=profile_dir, pid=pid)
            cls._sessions[normalized_work_key] = session
            return session

    @classmethod
    def mark_healthy(cls, work_key: str, *, target_id: str = "", target_url: str = "") -> None:
        normalized_work_key = cls.normalize_work_key(work_key)
        with cls._lock:
            session = cls._sessions.get(normalized_work_key)
            if session is None:
                return
            session.last_heartbeat_at = _time()
            session.last_error_code = ""
            if target_id:
                session.last_target_id = target_id
            if target_url:
                session.last_target_url = target_url

    @classmethod
    def mark_error(cls, work_key: str, *, error_code: str = "") -> None:
        normalized_work_key = cls.normalize_work_key(work_key)
        with cls._lock:
            session = cls._sessions.get(normalized_work_key)
            if session is None:
                return
            session.last_heartbeat_at = _time()
            session.last_error_code = str(error_code or "")

    @classmethod
    def release(cls, work_key: str) -> None:
        with cls._lock:
            cls._sessions.pop(cls.normalize_work_key(work_key), None)

    @classmethod
    def get_all(cls) -> dict[str, CDPSession]:
        return dict(cls._sessions)


def _next_id() -> int:
    """CDP 메시지 ID 순차 생성."""
    global _MSG_ID
    _MSG_ID += 1
    return _MSG_ID


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_port(value: Any, default: int = CDP_PORT) -> int:
    try:
        port = int(value)
    except Exception:
        return default
    if 1 <= port <= 65535:
        return port
    return default


def _effective_port(params: Dict[str, Any] | None = None) -> int:
    if isinstance(params, dict) and "port" in params:
        return _coerce_port(params.get("port"), CDP_PORT)
    if isinstance(params, dict):
        work_key = CDPSessionManager.normalize_work_key(params.get("work_key", "general"))
        session = CDPSessionManager.get_session(work_key)
        if session:
            return session.port
    general = CDPSessionManager.get_session("general")
    if general:
        return general.port
    return CDP_PORT


async def _http_get_json(port: int, path: str, timeout: float = 2.0) -> Any:
    def _fetch() -> Any:
        url = f"http://{CDP_HOST}:{port}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="ignore"))

    return await asyncio.to_thread(_fetch)


async def _probe_cdp_version(port: int) -> Dict[str, Any] | None:
    try:
        payload = await _http_get_json(port, "/json/version", timeout=1.5)
        if isinstance(payload, dict) and payload.get("webSocketDebuggerUrl"):
            return payload
    except Exception:
        return None
    return None


async def _list_cdp_targets(port: int) -> list[dict[str, Any]]:
    payload = await _http_get_json(port, "/json", timeout=2.0)
    if not isinstance(payload, list):
        raise ConnectionError(f"CDP /json 응답이 list가 아닙니다 (port={port})")
    return payload


async def _is_port_open(port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection(CDP_HOST, port)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((CDP_HOST, 0))
        return int(sock.getsockname()[1])


async def _wait_cdp_ready(port: int, timeout_seconds: float) -> Dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.1)
    while asyncio.get_running_loop().time() < deadline:
        info = await _probe_cdp_version(port)
        if info is not None:
            return info
        await asyncio.sleep(0.3)
    return None


def _default_profile_root() -> str:
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "KakaoBot",
            "cdp-profile",
        )
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), ".kakaobot-cdp-profile")
    return os.path.join(os.path.expanduser("~"), ".kakaobot-cdp-profile")


def _resolve_profile_dir(params: Dict[str, Any]) -> str:
    explicit = str(params.get("user_data_dir", "") or "").strip()
    if explicit:
        return explicit
    root = _default_profile_root()
    if not _as_bool(params.get("isolated_profile", False), default=False):
        return root
    isolation_id = str(params.get("isolation_id", "") or "").strip() or uuid.uuid4().hex[:8]
    return os.path.join(root, f"isolated-{isolation_id}")


def _candidate_ports(params: Dict[str, Any]) -> list[int]:
    preferred = _coerce_port(params.get("preferred_port", params.get("port", CDP_PORT)), CDP_PORT)
    candidates: list[int] = [preferred]

    raw_candidates = params.get("port_candidates")
    if isinstance(raw_candidates, list):
        for value in raw_candidates:
            port = _coerce_port(value, 0)
            if port and port not in candidates:
                candidates.append(port)

    if _as_bool(params.get("dynamic_port", False), default=False):
        for fallback in (9222, 9333, 9444, 9555, 9666, 9777):
            if fallback not in candidates:
                candidates.append(fallback)
        random_free = _find_free_port()
        if random_free not in candidates:
            candidates.append(random_free)

    return candidates


def _work_key_from_params(params: Dict[str, Any] | None = None) -> str:
    if not isinstance(params, dict):
        return CDPSessionManager.normalize_work_key("general")
    return CDPSessionManager.normalize_work_key(params.get("work_key", "general"))


def _resolve_timeout(
    params: Dict[str, Any] | None,
    *,
    param_name: str,
    default: float,
    minimum: float = 1.0,
    maximum: float = 60.0,
) -> float:
    raw_value = default
    if isinstance(params, dict):
        raw_value = params.get(param_name, params.get("command_timeout_seconds", default))
    try:
        timeout = float(raw_value or default)
    except Exception:
        timeout = float(default)
    timeout = max(minimum, min(timeout, maximum))
    if isinstance(params, dict):
        try:
            outer_timeout = float(params.get("command_timeout_seconds", 0) or 0)
        except Exception:
            outer_timeout = 0.0
        if outer_timeout > 1.0:
            timeout = min(timeout, max(minimum, outer_timeout - 1.0))
    return timeout


def _resolve_command_timeout_budget(
    params: Dict[str, Any] | None,
    *,
    default: float = CDP_COMMAND_TIMEOUT_SECONDS,
    minimum: float = 1.0,
    maximum: float = 300.0,
) -> float:
    raw_value = default
    if isinstance(params, dict):
        raw_value = params.get("command_timeout_seconds", params.get("timeout", default))
    try:
        timeout = float(raw_value or default)
    except Exception:
        timeout = float(default)
    return max(minimum, min(timeout, maximum))


def _looks_like_crashed_target(url: str, title: str) -> bool:
    haystack = f"{url} {title}".lower()
    return any(token in haystack for token in ("chrome-error://", "chrome://crash", "aw, snap", "target crashed"))


def _looks_like_translated_target(url: str, title: str) -> bool:
    haystack = f"{url} {title}".lower()
    return "translate.google" in haystack or "translate.goog" in haystack or "google translate" in haystack


def _looks_like_vvic_target(url: str, title: str) -> bool:
    haystack = f"{url} {title}".lower()
    return "vvic" in haystack


def _target_sort_key(target: dict[str, Any], *, preferred_target_id: str = "") -> tuple[int, int, int, int, str]:
    target_id = str(target.get("targetId") or target.get("id") or "")
    url = str(target.get("url") or "")
    title = str(target.get("title") or "")
    return (
        0 if preferred_target_id and target_id == preferred_target_id else 1,
        1 if _looks_like_crashed_target(url, title) else 0,
        1 if _looks_like_translated_target(url, title) else 0,
        1 if url.strip().lower() in {"", "about:blank", "chrome://newtab/"} else 0,
        target_id,
    )


async def _get_browser_ws_url(port: int) -> str:
    version = await _probe_cdp_version(port)
    if version is None:
        raise CDPCommandError(
            _ERROR_CDP_NOT_READY,
            f"http://{CDP_HOST}:{port}/json/version 응답 없음",
            details={"port": port},
        )
    ws_url = str(version.get("webSocketDebuggerUrl", "") or "").strip()
    if not ws_url:
        raise CDPCommandError(
            _ERROR_CDP_NOT_READY,
            f"webSocketDebuggerUrl 누락 (port={port})",
            details={"port": port},
        )
    return ws_url


async def _select_page_targets(
    port: int,
    *,
    target_id: str = "",
    target_idx: int = 0,
) -> list[dict[str, Any]]:
    try:
        targets = await _list_cdp_targets(port)
    except Exception as exc:
        raise CDPCommandError(
            _ERROR_CDP_NOT_READY,
            f"CDP target 목록 조회 실패: {exc}",
            details={"port": port},
        ) from exc
    pages: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        ws_url = str(target.get("webSocketDebuggerUrl") or "").strip()
        target_type = str(target.get("type") or "").strip().lower()
        if target_type and target_type != "page":
            continue
        if not ws_url:
            continue
        enriched = dict(target)
        enriched["targetId"] = str(target.get("targetId") or target.get("id") or "")
        pages.append(enriched)
    if not pages:
        raise CDPCommandError(
            _ERROR_STALE_TARGET,
            f"usable page target 없음 (port={port})",
            details={"port": port},
        )

    if target_id:
        exact = [page for page in pages if str(page.get("targetId") or "") == target_id]
        if exact:
            pages = exact + [page for page in pages if page not in exact]
    else:
        pages = sorted(pages, key=lambda target: _target_sort_key(target))
        if 0 <= target_idx < len(pages):
            selected = pages[target_idx]
            pages = [selected] + [page for page in pages if page is not selected]

    healthy = [
        page for page in pages
        if not _looks_like_crashed_target(str(page.get("url") or ""), str(page.get("title") or ""))
    ]
    return healthy or pages


def _classify_cdp_error(method: str, message: str) -> str:
    lowered = str(message or "").lower()
    if "syntaxerror" in lowered or "unexpected token" in lowered:
        return _ERROR_SYNTAX_ERROR
    if any(
        token in lowered
        for token in (
            "cannot find context with specified id",
            "target closed",
            "session closed",
            "target detached",
            "execution context was destroyed",
            "inspector.targetcrashed",
        )
    ):
        return _ERROR_STALE_TARGET
    if "timed out" in lowered or "timeout" in lowered:
        return _ERROR_RUNTIME_EVALUATE_TIMEOUT if method == "Runtime.evaluate" else _ERROR_CDP_NOT_READY
    return _ERROR_CDP_NOT_READY


def _cdp_error_from_payload(method: str, payload: dict[str, Any]) -> CDPCommandError:
    message = str(payload.get("message") or payload)
    code = _classify_cdp_error(method, message)
    return CDPCommandError(code, f"{method} failed: {message}", details={"method": method, "cdp_error": payload})


async def _recv_cdp_response(
    ws: Any,
    *,
    msg_id: int,
    method: str,
    timeout_seconds: float,
    session_id: str = "",
) -> Dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.1)
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            code = _ERROR_RUNTIME_EVALUATE_TIMEOUT if method == "Runtime.evaluate" else _ERROR_CDP_NOT_READY
            raise CDPCommandError(
                code,
                f"{method} timed out after {timeout_seconds:.1f}s",
                details={"method": method, "timeout_seconds": timeout_seconds},
            )
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        response = json.loads(raw)
        if response.get("id") == msg_id:
            if "error" in response:
                raise _cdp_error_from_payload(method, response["error"])
            return response.get("result", {})

        event_method = str(response.get("method") or "")
        if event_method == "Inspector.targetCrashed":
            raise CDPCommandError(
                _ERROR_STALE_TARGET,
                "Chrome tab crashed while waiting for CDP response",
                details={"event": event_method, "method": method},
            )
        if event_method == "Target.detachedFromTarget":
            event_params = response.get("params") or {}
            detached_session_id = str(event_params.get("sessionId") or "")
            if session_id and detached_session_id == session_id:
                reason = str(event_params.get("reason") or "target detached")
                raise CDPCommandError(
                    _ERROR_STALE_TARGET,
                    f"Target detached: {reason}",
                    details={"event": event_method, "detach_reason": reason, "method": method},
                )


async def _request_cdp(
    ws: Any,
    method: str,
    params: Dict[str, Any] | None = None,
    *,
    timeout_seconds: float,
    session_id: str = "",
) -> Dict[str, Any]:
    msg_id = _next_id()
    payload: Dict[str, Any] = {"id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    if session_id:
        payload["sessionId"] = session_id
    await asyncio.wait_for(ws.send(json.dumps(payload)), timeout=max(1.0, min(timeout_seconds, 5.0)))
    return await _recv_cdp_response(
        ws,
        msg_id=msg_id,
        method=method,
        timeout_seconds=timeout_seconds,
        session_id=session_id,
    )


async def _send_cdp(
    ws_url: str,
    method: str,
    params: Dict[str, Any] | None = None,
    *,
    timeout_seconds: float = CDP_COMMAND_TIMEOUT_SECONDS,
    session_id: str = "",
) -> Dict[str, Any]:
    import websockets

    async with websockets.connect(
        ws_url,
        open_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
        close_timeout=1,
        max_size=10 * 1024 * 1024,
    ) as ws:
        return await _request_cdp(
            ws,
            method,
            params,
            timeout_seconds=timeout_seconds,
            session_id=session_id,
        )


async def _send_cdp_command(
    port: int,
    method: str,
    params: Dict[str, Any] | None = None,
    *,
    timeout_seconds: float,
    target_id: str = "",
    target_idx: int = 0,
) -> Dict[str, Any]:
    import websockets

    browser_ws_url = await _get_browser_ws_url(port)
    if method.startswith("Browser.") or method.startswith("Target."):
        return await _send_cdp(browser_ws_url, method, params, timeout_seconds=timeout_seconds)

    candidates = await _select_page_targets(port, target_id=target_id, target_idx=target_idx)
    attempts = candidates[: max(1, min(len(candidates), CDP_RECOVERY_RETRY_LIMIT + 1))]
    last_error: CDPCommandError | None = None

    for candidate in attempts:
        candidate_target_id = str(candidate.get("targetId") or candidate.get("id") or "")
        candidate_url = str(candidate.get("url") or "")
        candidate_title = str(candidate.get("title") or "")
        try:
            async with websockets.connect(
                browser_ws_url,
                open_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
                close_timeout=1,
                max_size=10 * 1024 * 1024,
            ) as ws:
                try:
                    await _request_cdp(
                        ws,
                        "Target.activateTarget",
                        {"targetId": candidate_target_id},
                        timeout_seconds=min(3.0, timeout_seconds),
                    )
                except CDPCommandError:
                    pass

                attach = await _request_cdp(
                    ws,
                    "Target.attachToTarget",
                    {"targetId": candidate_target_id, "flatten": True},
                    timeout_seconds=min(3.0, timeout_seconds),
                )
                session_id = str(attach.get("sessionId") or "")
                if not session_id:
                    raise CDPCommandError(
                        _ERROR_STALE_TARGET,
                        f"target attach 실패: {candidate_target_id}",
                        details={"target_id": candidate_target_id, "url": candidate_url},
                    )
                try:
                    result = await _request_cdp(
                        ws,
                        method,
                        params,
                        timeout_seconds=timeout_seconds,
                        session_id=session_id,
                    )
                    result["_target"] = {
                        "id": candidate_target_id,
                        "url": candidate_url,
                        "title": candidate_title,
                    }
                    return result
                finally:
                    try:
                        await _request_cdp(
                            ws,
                            "Target.detachFromTarget",
                            {"sessionId": session_id},
                            timeout_seconds=1.5,
                        )
                    except Exception:
                        pass
        except CDPCommandError as exc:
            last_error = exc
            if exc.code not in {_ERROR_STALE_TARGET, _ERROR_RUNTIME_EVALUATE_TIMEOUT, _ERROR_CDP_NOT_READY}:
                raise

    if last_error is not None:
        raise last_error
    raise CDPCommandError(
        _ERROR_STALE_TARGET,
        f"usable page target 없음 (port={port})",
        details={"port": port},
    )


async def _collect_page_diagnostics(
    port: int,
    *,
    timeout_seconds: float,
    target_id: str = "",
    target_idx: int = 0,
) -> dict[str, Any]:
    js = """
    (() => {
      const selectors = [
        '[data-product-id]',
        '[data-goods-id]',
        '.search-list .item',
        '.goods-list .item',
        '.goods-list-item',
        '.search-result-item',
        '[class*="goods-item"]',
        '[class*="product-card"]'
      ];
      let cardCount = 0;
      let matchedSelector = '';
      for (const selector of selectors) {
        try {
          const count = document.querySelectorAll(selector).length;
          if (count > 0) {
            cardCount = count;
            matchedSelector = selector;
            break;
          }
        } catch (err) {}
      }
      const bodyText = (document.body && (document.body.innerText || document.body.textContent) || '').trim();
      return {
        readyState: document.readyState || '',
        href: String(location.href || ''),
        title: document.title || '',
        bodyTextLength: bodyText.length,
        matchedSelector,
        cardCount
      };
    })()
    """
    try:
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
            target_id=target_id,
            target_idx=target_idx,
        )
    except CDPCommandError as exc:
        return {"error_code": exc.code, "error": str(exc)}

    payload = result.get("result", {})
    value = payload.get("value")
    diagnostics = value if isinstance(value, dict) else {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    if isinstance(result.get("_target"), dict):
        diagnostics.setdefault("target_id", result["_target"].get("id"))
        diagnostics.setdefault("target_url", result["_target"].get("url"))
    return diagnostics


def _is_spa_shell_only(diagnostics: dict[str, Any]) -> bool:
    url = str(diagnostics.get("href") or diagnostics.get("target_url") or "")
    title = str(diagnostics.get("title") or "")
    if not _looks_like_vvic_target(url, title):
        return False
    ready_state = str(diagnostics.get("readyState") or "").lower()
    body_text_length = int(diagnostics.get("bodyTextLength") or 0)
    card_count = int(diagnostics.get("cardCount") or 0)
    return ready_state in {"interactive", "complete"} and card_count == 0 and body_text_length < VVIC_SPA_MIN_TEXT_LENGTH


def _record_cdp_success(params: Dict[str, Any] | None, target: dict[str, Any] | None = None) -> None:
    work_key = _work_key_from_params(params)
    target_payload = dict(target or {})
    CDPSessionManager.mark_healthy(
        work_key,
        target_id=str(target_payload.get("id") or ""),
        target_url=str(target_payload.get("url") or ""),
    )


def _command_error_response(port: int, params: Dict[str, Any] | None, exc: CDPCommandError) -> Dict[str, Any]:
    work_key = _work_key_from_params(params)
    CDPSessionManager.mark_error(work_key, error_code=exc.code)
    if exc.code in {_ERROR_CDP_NOT_READY, _ERROR_RUNTIME_EVALUATE_TIMEOUT, _ERROR_STALE_TARGET}:
        CDPSessionManager.release(work_key)
    data = {"error": str(exc), "error_code": exc.code, "port": port}
    data.update(exc.details)
    data.setdefault("work_key", work_key)
    return {"status": "error", "data": data}


async def _diagnose_cdp_failure(
    port: int,
    params: Dict[str, Any] | None,
    exc: CDPCommandError,
) -> CDPCommandError:
    if exc.code not in {_ERROR_CDP_NOT_READY, _ERROR_RUNTIME_EVALUATE_TIMEOUT, _ERROR_STALE_TARGET}:
        return exc
    try:
        diagnostics = await _collect_page_diagnostics(
            port,
            timeout_seconds=min(4.0, CDP_EVALUATE_TIMEOUT_SECONDS),
            target_id=str((params or {}).get("target_id") or ""),
            target_idx=int((params or {}).get("target_idx", 0) or 0),
        )
    except Exception:
        return exc
    if not diagnostics:
        return exc
    if _is_spa_shell_only(diagnostics):
        return CDPCommandError(
            _ERROR_SPA_SHELL_ONLY,
            "VVIC 검색 페이지가 SPA 셸만 반환했습니다.",
            details={"diagnostics": diagnostics},
        )
    enriched = dict(exc.details)
    enriched["diagnostics"] = diagnostics
    return CDPCommandError(exc.code, str(exc), details=enriched)


def _decode_runtime_value(result: Dict[str, Any]) -> Any:
    payload = result.get("result", {}) if isinstance(result, dict) else {}
    value = payload.get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _runtime_exception_error(result: Dict[str, Any]) -> CDPCommandError | None:
    payload = result.get("result", {}) if isinstance(result, dict) else {}
    exception = payload.get("exceptionDetails") if isinstance(payload, dict) else None
    if not isinstance(exception, dict):
        return None
    text = str(exception.get("text") or "")
    details = exception.get("exception") if isinstance(exception.get("exception"), dict) else {}
    class_name = str(details.get("className") or "")
    description = str(details.get("description") or payload.get("description") or text or "JS 실행 오류")
    code = _ERROR_SYNTAX_ERROR if class_name == "SyntaxError" or "syntaxerror" in description.lower() else _classify_cdp_error("Runtime.evaluate", description)
    return CDPCommandError(code, description, details={"exceptionDetails": exception})


def _chrome_not_running_error(port: int) -> Dict[str, Any]:
    return {
        "status": "error",
        "data": {
            "error": f"Chrome이 CDP 모드로 준비되지 않았습니다 (port {port})",
            "error_code": "CDP_NOT_READY",
            "hint": "browser_launch 명령으로 Chrome을 시작하세요",
            "port": port,
        },
    }


# ── 커맨드 핸들러 ─────────────────────────────────────────────────────────

async def browser_navigate(params: Dict[str, Any]) -> Dict[str, Any]:
    """URL 이동. params: url(필수)"""
    url = params.get("url", "")
    if not url:
        return {"status": "error", "data": {"error": "url 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        timeout_seconds = _resolve_timeout(params, param_name="page_timeout_seconds", default=CDP_COMMAND_TIMEOUT_SECONDS, maximum=90.0)
        result = await _send_cdp_command(port, "Page.navigate", {"url": url}, timeout_seconds=timeout_seconds)
        _record_cdp_success(params, result.get("_target"))
        logger.info("브라우저 이동: %s", url)
        target = result.get("_target", {}) if isinstance(result, dict) else {}
        return {
            "status": "success",
            "data": {
                "url": url,
                "frameId": result.get("frameId", ""),
                "target_id": target.get("id"),
                "target_url": target.get("url"),
            },
        }
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_click(params: Dict[str, Any]) -> Dict[str, Any]:
    """CSS 셀렉터 클릭. params: selector(필수)"""
    selector = params.get("selector", "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        # querySelector로 노드 찾기 → 좌표 계산 → 클릭
        js = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
            var rect = el.getBoundingClientRect();
            var x = rect.left + rect.width / 2;
            var y = rect.top + rect.height / 2;
            el.click();
            return JSON.stringify({{"x": x, "y": y, "clicked": true}});
        }})()
        """
        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=30.0)
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        data = _decode_runtime_value(result)
        if isinstance(data, dict) and data.get("error"):
            return {"status": "error", "data": data}
        _record_cdp_success(params, result.get("_target"))
        logger.info("브라우저 클릭: %s", selector)
        return {"status": "success", "data": data}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_fill(params: Dict[str, Any]) -> Dict[str, Any]:
    """입력 필드에 텍스트 입력. params: selector(필수), value(필수)"""
    selector = params.get("selector", "")
    value = params.get("value", "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        js = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
            el.focus();
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{"filled": true, "selector": {json.dumps(selector)}}});
        }})()
        """
        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=30.0)
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        data = _decode_runtime_value(result)
        if isinstance(data, dict) and data.get("error"):
            return {"status": "error", "data": data}
        _record_cdp_success(params, result.get("_target"))
        logger.info("브라우저 입력: %s", selector)
        return {"status": "success", "data": data}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_press_key(params: Dict[str, Any]) -> Dict[str, Any]:
    """키 입력. params: key(필수), selector(선택)."""
    key = str(params.get("key", "") or "")
    selector = str(params.get("selector", "") or "")
    if not key:
        return {"status": "error", "data": {"error": "key 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=30.0)
        target_info: dict[str, Any] | None = None
        if selector:
            focus_js = f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
                el.focus();
                return JSON.stringify({{"focused": true}});
            }})()
            """
            focus = await _send_cdp_command(
                port,
                "Runtime.evaluate",
                {"expression": focus_js, "returnByValue": True, "awaitPromise": False},
                timeout_seconds=timeout_seconds,
            )
            error = _runtime_exception_error(focus)
            if error is not None:
                raise error
            data = _decode_runtime_value(focus)
            if isinstance(data, dict) and data.get("error"):
                return {"status": "error", "data": data}
            target_info = focus.get("_target") if isinstance(focus.get("_target"), dict) else None

        if len(key) == 1:
            result = await _send_cdp_command(port, "Input.insertText", {"text": key}, timeout_seconds=timeout_seconds)
        else:
            result = await _send_cdp_command(port, "Input.dispatchKeyEvent", {"type": "keyDown", "key": key}, timeout_seconds=timeout_seconds)
            await _send_cdp_command(port, "Input.dispatchKeyEvent", {"type": "keyUp", "key": key}, timeout_seconds=timeout_seconds)
        _record_cdp_success(params, target_info or result.get("_target"))
        return {"status": "success", "data": {"key": key, "selector": selector}}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_select_option(params: Dict[str, Any]) -> Dict[str, Any]:
    """select 옵션 선택. params: selector(필수), value(필수: 문자열 또는 목록)."""
    selector = str(params.get("selector", "") or "")
    value = params.get("value")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}
    if value is None:
        return {"status": "error", "data": {"error": "value 파라미터가 필요합니다"}}

    values = value if isinstance(value, list) else [value]
    values = [str(v) for v in values]
    port = _effective_port(params)
    try:
        js = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
            if (el.tagName.toLowerCase() !== 'select') return JSON.stringify({{"error": "select 요소가 아닙니다: " + {json.dumps(selector)}}});
            var wanted = new Set({json.dumps(values)});
            var matched = [];
            for (var option of el.options) {{
                var ok = wanted.has(option.value) || wanted.has((option.textContent || '').trim());
                if (el.multiple) option.selected = ok;
                else if (ok) el.value = option.value;
                if (ok) matched.push(option.value);
            }}
            if (!matched.length) return JSON.stringify({{"error": "일치하는 옵션이 없습니다", "value": {json.dumps(values)}}});
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{"selected": matched, "selector": {json.dumps(selector)}}});
        }})()
        """
        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=30.0)
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        data = _decode_runtime_value(result)
        if isinstance(data, dict) and data.get("error"):
            return {"status": "error", "data": data}
        _record_cdp_success(params, result.get("_target"))
        return {"status": "success", "data": data}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_check(params: Dict[str, Any]) -> Dict[str, Any]:
    """체크박스/라디오 상태 설정. params: selector(필수), checked(기본 true)."""
    selector = str(params.get("selector", "") or "")
    checked = _as_bool(params.get("checked", True), default=True)
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        js = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
            if (!('checked' in el)) return JSON.stringify({{"error": "checked 속성이 없는 요소입니다: " + {json.dumps(selector)}}});
            var desired = {json.dumps(checked)};
            if (Boolean(el.checked) !== desired) el.click();
            el.checked = desired;
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{"selector": {json.dumps(selector)}, "checked": Boolean(el.checked)}});
        }})()
        """
        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=30.0)
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        data = _decode_runtime_value(result)
        if isinstance(data, dict) and data.get("error"):
            return {"status": "error", "data": data}
        _record_cdp_success(params, result.get("_target"))
        return {"status": "success", "data": data}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_file_upload(params: Dict[str, Any]) -> Dict[str, Any]:
    """file input에 PC 로컬 파일 지정. params: selector(필수), file_paths 또는 file_path."""
    selector = str(params.get("selector", "") or "")
    raw_paths = params.get("file_paths", params.get("file_path", ""))
    file_paths = raw_paths if isinstance(raw_paths, list) else [raw_paths]
    file_paths = [os.path.abspath(os.path.expanduser(str(p))) for p in file_paths if str(p or "").strip()]
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}
    if not file_paths:
        return {"status": "error", "data": {"error": "file_paths 파라미터가 필요합니다"}}
    missing = [p for p in file_paths if not os.path.isfile(p)]
    if missing:
        return {"status": "error", "data": {"error": "파일을 찾을 수 없습니다", "missing": missing}}

    port = _effective_port(params)
    try:
        timeout_seconds = _resolve_timeout(params, param_name="page_timeout_seconds", default=CDP_COMMAND_TIMEOUT_SECONDS, maximum=45.0)
        doc = await _send_cdp_command(
            port,
            "DOM.getDocument",
            {"depth": -1, "pierce": True},
            timeout_seconds=timeout_seconds,
        )
        root_id = doc.get("root", {}).get("nodeId")
        node = await _send_cdp_command(
            port,
            "DOM.querySelector",
            {"nodeId": root_id, "selector": selector},
            timeout_seconds=timeout_seconds,
        )
        node_id = node.get("nodeId")
        if not node_id:
            return {"status": "error", "data": {"error": f"요소를 찾을 수 없습니다: {selector}"}}
        await _send_cdp_command(
            port,
            "DOM.setFileInputFiles",
            {"nodeId": node_id, "files": file_paths},
            timeout_seconds=timeout_seconds,
        )
        _record_cdp_success(params, node.get("_target") or doc.get("_target"))
        return {"status": "success", "data": {"selector": selector, "files": file_paths, "count": len(file_paths)}}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_download(params: Dict[str, Any]) -> Dict[str, Any]:
    """다운로드를 유발하는 요소를 클릭하고 PC 다운로드 파일을 감지한다."""
    selector = str(params.get("selector", "") or "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}
    download_dir = str(params.get("download_dir", "") or "").strip()
    if not download_dir:
        download_dir = os.path.join(os.path.expanduser("~"), "AADSDownloads")
    download_dir = os.path.abspath(os.path.expanduser(download_dir))
    timeout_seconds = float(params.get("timeout_seconds", 60) or 60)

    port = _effective_port(params)
    try:
        os.makedirs(download_dir, exist_ok=True)
        before = {name: os.path.getmtime(os.path.join(download_dir, name)) for name in os.listdir(download_dir)}
        try:
            await _send_cdp_command(
                port,
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": download_dir, "eventsEnabled": True},
                timeout_seconds=min(max(timeout_seconds, 5.0), 30.0),
            )
        except CDPCommandError as exc:
            logger.warning("download behavior setup failed: %s", exc)

        click_result = await browser_click({**params, "port": port, "selector": selector})
        if click_result.get("status") != "success":
            return click_result

        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 1)
        while asyncio.get_running_loop().time() < deadline:
            candidates = []
            for name in os.listdir(download_dir):
                if name.endswith((".crdownload", ".tmp")):
                    continue
                path = os.path.join(download_dir, name)
                if not os.path.isfile(path):
                    continue
                mtime = os.path.getmtime(path)
                if name not in before or mtime > before.get(name, 0):
                    candidates.append((mtime, path))
            if candidates:
                candidates.sort(reverse=True)
                path = candidates[0][1]
                return {"status": "success", "data": {"path": path, "size": os.path.getsize(path), "download_dir": download_dir}}
            await asyncio.sleep(0.5)
        return {"status": "error", "data": {"error": "다운로드 파일 감지 시간 초과", "download_dir": download_dir}}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_screenshot(params: Dict[str, Any]) -> Dict[str, Any]:
    """브라우저 스크린샷. CDP Page.captureScreenshot → base64."""
    port = _effective_port(params)
    try:
        fmt = params.get("format", "png")
        quality = params.get("quality", 80)
        cdp_params: Dict[str, Any] = {"format": fmt}
        if fmt == "jpeg":
            cdp_params["quality"] = quality
        timeout_seconds = _resolve_timeout(params, param_name="page_timeout_seconds", default=CDP_COMMAND_TIMEOUT_SECONDS, maximum=45.0)
        result = await _send_cdp_command(port, "Page.captureScreenshot", cdp_params, timeout_seconds=timeout_seconds)
        img_data = result.get("data", "")
        _record_cdp_success(params, result.get("_target"))
        logger.info("브라우저 스크린샷 캡처 (%s)", fmt)
        return {
            "status": "success",
            "data": {"screenshot_base64": img_data, "format": fmt},
        }
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_get_text(params: Dict[str, Any]) -> Dict[str, Any]:
    """페이지 또는 셀렉터 텍스트 추출. params: selector(선택)"""
    port = _effective_port(params)
    try:
        selector = params.get("selector", "")
        if selector:
            js = f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다"}});
                return JSON.stringify({{"text": el.innerText || el.textContent}});
            }})()
            """
        else:
            js = "JSON.stringify({text: document.body.innerText})"

        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=30.0)
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        data = _decode_runtime_value(result)
        if isinstance(data, dict) and data.get("error"):
            return {"status": "error", "data": data}
        _record_cdp_success(params, result.get("_target"))
        diagnostics = await _collect_page_diagnostics(
            port,
            timeout_seconds=min(3.0, timeout_seconds),
            target_id=str((result.get("_target") or {}).get("id") or ""),
        )
        if _is_spa_shell_only(diagnostics):
            spa_error = CDPCommandError(
                _ERROR_SPA_SHELL_ONLY,
                "VVIC 검색 페이지가 SPA 셸만 반환했습니다.",
                details={"diagnostics": diagnostics},
            )
            return _command_error_response(port, params, spa_error)
        return {"status": "success", "data": data}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_eval(params: Dict[str, Any]) -> Dict[str, Any]:
    """JavaScript 실행. params: expression(필수). 로컬 PC 전용, 로그 필수."""
    expression = params.get("expression", "")
    if not expression:
        return {"status": "error", "data": {"error": "expression 파라미터가 필요합니다"}}

    logger.info("브라우저 JS 실행: %s", expression[:200])

    port = _effective_port(params)
    started_at = _time()
    command_budget_seconds = _resolve_command_timeout_budget(
        params,
        default=max(CDP_EVALUATE_TIMEOUT_SECONDS + 2.0, CDP_COMMAND_TIMEOUT_SECONDS),
        minimum=1.0,
        maximum=300.0,
    )
    try:
        timeout_seconds = _resolve_timeout(
            params,
            param_name="evaluate_timeout_seconds",
            default=CDP_EVALUATE_TIMEOUT_SECONDS,
            maximum=30.0,
        )
        timeout_seconds = min(timeout_seconds, max(1.0, command_budget_seconds - 0.5))
        try:
            result = await asyncio.wait_for(
                _send_cdp_command(
                    port,
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                    timeout_seconds=timeout_seconds,
                    target_id=str(params.get("target_id") or ""),
                    target_idx=int(params.get("target_idx", 0) or 0),
                ),
                timeout=max(0.5, timeout_seconds + 0.25),
            )
        except asyncio.TimeoutError as exc:
            raise CDPCommandError(
                _ERROR_RUNTIME_EVALUATE_TIMEOUT,
                f"Runtime.evaluate timed out after {timeout_seconds:.1f}s",
                details={"timeout_seconds": timeout_seconds},
            ) from exc
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        res_data = result.get("result", {})
        if res_data.get("subtype") == "error":
            raise CDPCommandError(_ERROR_CDP_NOT_READY, str(res_data.get("description", "JS 실행 오류")))
        _record_cdp_success(params, result.get("_target"))
        diagnostics: dict[str, Any] = {}
        elapsed = max(0.0, _time() - started_at)
        remaining_budget = command_budget_seconds - elapsed
        if remaining_budget > 0.7:
            diagnostics_timeout = min(3.0, timeout_seconds, max(0.5, remaining_budget - 0.25))
            diagnostics = await _collect_page_diagnostics(
                port,
                timeout_seconds=diagnostics_timeout,
                target_id=str((result.get("_target") or {}).get("id") or ""),
            )
        if _is_spa_shell_only(diagnostics):
            spa_error = CDPCommandError(
                _ERROR_SPA_SHELL_ONLY,
                "VVIC 검색 페이지가 SPA 셸만 반환했습니다.",
                details={"diagnostics": diagnostics},
            )
            return _command_error_response(port, params, spa_error)
        target = result.get("_target", {}) if isinstance(result, dict) else {}
        return {
            "status": "success",
            "data": {
                "value": res_data.get("value"),
                "type": res_data.get("type", ""),
                "target_id": target.get("id"),
                "target_url": target.get("url"),
                "diagnostics": diagnostics,
            },
        }
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, exc)
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def browser_tabs(params: Dict[str, Any]) -> Dict[str, Any]:
    """열린 탭 목록."""
    port = _effective_port(params)
    try:
        targets = await _list_cdp_targets(port)
        tabs = [
            {"id": t.get("id", ""), "title": t.get("title", ""), "url": t.get("url", ""), "type": t.get("type", "")}
            for t in targets if t.get("type") == "page" or t.get("webSocketDebuggerUrl")
        ]
        return {"status": "success", "data": {"tabs": tabs, "count": len(tabs)}}
    except Exception:
        return _chrome_not_running_error(port)


async def browser_health(params: Dict[str, Any]) -> Dict[str, Any]:
    """CDP 세션 건강 확인 + stale 세션 정리. params: work_key(선택), cleanup(선택, 기본true)."""
    work_key = CDPSessionManager.normalize_work_key(params.get("work_key", "general"))
    do_cleanup = bool(params.get("cleanup", True))
    port = _effective_port(params)

    session = CDPSessionManager.get_session(work_key)
    if session:
        port = session.port

    version = await _probe_cdp_version(port)
    if version is None:
        if do_cleanup and session:
            CDPSessionManager.release(work_key)
            logger.info("browser_health: stale session 정리 (work_key=%s port=%d)", work_key, port)
        return {
            "status": "error",
            "data": {
                "error": f"CDP 응답 없음 (port {port})",
                "error_code": _ERROR_CDP_NOT_READY,
                "port": port,
                "work_key": work_key,
                "session_released": do_cleanup and session is not None,
            },
        }

    try:
        result = await _send_cdp_command(port, "Runtime.evaluate", {
            "expression": "JSON.stringify({readyState: document.readyState, href: location.href, bodyLen: document.body ? document.body.innerText.length : -1})",
            "returnByValue": True,
        }, timeout_seconds=5)
        value = result.get("result", {}).get("value", "{}")
        data = json.loads(value) if isinstance(value, str) else (value or {})
        return {
            "status": "success",
            "data": {
                "port": port,
                "work_key": work_key,
                "cdp_version": version.get("Browser", ""),
                "page": data,
            },
        }
    except CDPCommandError as e:
        if do_cleanup and session:
            CDPSessionManager.release(work_key)
        return {
            "status": "error",
            "data": {
                "error": str(e),
                "error_code": e.code,
                "port": port,
                "work_key": work_key,
                "session_released": do_cleanup and session is not None,
            },
        }
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY}}


async def browser_launch(params: Dict[str, Any]) -> Dict[str, Any]:
    """Chrome CDP 전용 세션 시작 (전용 프로필 + 동적 포트 충돌 회피)."""
    url = params.get("url", "about:blank")
    work_key = CDPSessionManager.normalize_work_key(str(params.get("work_key", "general")))

    # OS별 Chrome 경로
    if sys.platform == "win32":
        chrome_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        chrome_paths = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        chrome_paths = ["google-chrome", "chromium-browser", "chromium"]

    chrome_exe = params.get("chrome_path", "")
    if not chrome_exe:
        for p in chrome_paths:
            if os.path.isfile(p):
                chrome_exe = p
                break
        if not chrome_exe:
            chrome_exe = chrome_paths[0]  # 기본값 시도

    profile_dir = _resolve_profile_dir(params)
    ports = _candidate_ports(params)
    preferred = _coerce_port(params.get("preferred_port", params.get("port", CDP_PORT)), CDP_PORT)
    new_window = _as_bool(params.get("new_window", True), default=True)
    ready_timeout = float(params.get("ready_timeout_seconds", 15.0) or 15.0)

    try:
        os.makedirs(profile_dir, exist_ok=True)

        existing_session = CDPSessionManager.get_session(work_key)
        if existing_session:
            existing = await _probe_cdp_version(existing_session.port)
            if existing is not None:
                return {
                    "status": "success",
                    "data": {
                        "message": f"기존 CDP 세션 사용 (port {existing_session.port})",
                        "port": existing_session.port,
                        "user_data_dir": existing_session.profile_dir,
                        "cdp_ready": True,
                        "websocket_debugger_url": existing.get("webSocketDebuggerUrl", ""),
                    },
                }
            CDPSessionManager.release(work_key)

        first_port = CDPSessionManager.allocate_port(work_key, preferred=preferred, candidates=ports)
        ports = [first_port] + [p for p in ports if p != first_port]

        for port in ports:
            existing = await _probe_cdp_version(port)
            if existing is not None:
                owner = CDPSessionManager.get_by_port(port)
                if owner and owner.work_key == work_key:
                    CDPSessionManager.register(work_key, port, profile_dir, pid=owner.pid)
                    return {
                        "status": "success",
                        "data": {
                            "message": f"기존 CDP 세션 사용 (port {port})",
                            "port": port,
                            "user_data_dir": profile_dir,
                            "cdp_ready": True,
                            "websocket_debugger_url": existing.get("webSocketDebuggerUrl", ""),
                        },
                    }
                if work_key == "general" and owner is None:
                    CDPSessionManager.register(work_key, port, profile_dir)
                    return {
                        "status": "success",
                        "data": {
                            "message": f"기존 CDP 세션 사용 (port {port})",
                            "port": port,
                            "user_data_dir": profile_dir,
                            "cdp_ready": True,
                            "websocket_debugger_url": existing.get("webSocketDebuggerUrl", ""),
                        },
                    }
                # 다른 work_key 또는 외부 CDP가 이미 점유한 포트는 재사용하지 않는다.
                continue

            if await _is_port_open(port):
                # 열려 있지만 /json/version 미응답이면 다른 포트로 우회
                continue

            cmd = [
                chrome_exe,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if new_window:
                cmd.append("--new-window")
            cmd.append(url)

            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ready = await _wait_cdp_ready(port, ready_timeout)
            if ready is None:
                continue

            CDPSessionManager.register(work_key, port, profile_dir, pid=int(proc.pid or 0))
            logger.info("Chrome CDP 시작 완료 (port=%d profile=%s work_key=%s)", port, profile_dir, work_key)
            return {
                "status": "success",
                "data": {
                    "message": f"Chrome CDP 준비 완료 (port {port})",
                    "port": port,
                    "user_data_dir": profile_dir,
                    "cdp_ready": True,
                    "websocket_debugger_url": ready.get("webSocketDebuggerUrl", ""),
                },
            }

        if work_key != "general":
            # 명시적 업무 키는 기존 전역 9222를 훔쳐 쓰지 않고, OS 빈 포트로 한 번 더 격리 시도한다.
            port = _find_free_port()
            cmd = [
                chrome_exe,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if new_window:
                cmd.append("--new-window")
            cmd.append(url)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ready = await _wait_cdp_ready(port, ready_timeout)
            if ready is not None:
                CDPSessionManager.register(work_key, port, profile_dir, pid=int(proc.pid or 0))
                return {
                    "status": "success",
                    "data": {
                        "message": f"Chrome CDP 준비 완료 (port {port})",
                        "port": port,
                        "user_data_dir": profile_dir,
                        "cdp_ready": True,
                        "websocket_debugger_url": ready.get("webSocketDebuggerUrl", ""),
                    },
                }

        return {
            "status": "error",
            "data": {
                "error": "CDP endpoint 준비 실패 (/json/version 응답 없음)",
                "error_code": "CDP_NOT_READY",
                "port_candidates": ports,
                "hint": "포트를 변경하거나 이미 실행 중인 Chrome 충돌 여부를 확인하세요",
            },
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "data": {
                "error": f"Chrome을 찾을 수 없습니다: {chrome_exe}",
                "hint": "chrome_path 파라미터로 Chrome 경로를 지정하거나 Chrome을 설치하세요",
            },
        }
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": "CDP_NOT_READY"}}
