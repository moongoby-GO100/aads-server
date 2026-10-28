"""AADS: CDP 브라우저 자동화 — Chrome DevTools Protocol via WebSocket."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from time import monotonic as _monotonic
from time import time as _time
from typing import Any, Dict

logger = logging.getLogger(__name__)

CDP_HOST = "localhost"
CDP_PORT = 9222
CDP_CONNECT_TIMEOUT_SECONDS = float(os.getenv("AADS_CDP_CONNECT_TIMEOUT_SECONDS", "12") or "12")
CDP_COMMAND_TIMEOUT_SECONDS = float(os.getenv("AADS_CDP_COMMAND_TIMEOUT_SECONDS", "45") or "45")
CDP_EVALUATE_TIMEOUT_SECONDS = float(os.getenv("AADS_CDP_EVALUATE_TIMEOUT_SECONDS", "45") or "45")
CDP_RECOVERY_RETRY_LIMIT = max(0, int(os.getenv("AADS_CDP_RECOVERY_RETRY_LIMIT", "2") or "2"))
CDP_WS_CONNECT_RETRY_LIMIT = max(0, int(os.getenv("AADS_CDP_WS_CONNECT_RETRY_LIMIT", "2") or "2"))
CDP_COMMAND_GUARD_WAIT_SECONDS = float(os.getenv("AADS_CDP_COMMAND_GUARD_WAIT_SECONDS", "4.0") or "4.0")
CDP_COMMAND_GUARD_STALE_SECONDS = float(os.getenv("AADS_CDP_COMMAND_GUARD_STALE_SECONDS", "45") or "45")
VVIC_SPA_MIN_TEXT_LENGTH = max(40, int(os.getenv("AADS_VVIC_SPA_MIN_TEXT_LENGTH", "120") or "120"))
_ERROR_CDP_NOT_READY = "CDP_NOT_READY"
_ERROR_RUNTIME_EVALUATE_TIMEOUT = "RUNTIME_EVALUATE_TIMEOUT"
_ERROR_STALE_TARGET = "STALE_TARGET"
_ERROR_SYNTAX_ERROR = "SYNTAX_ERROR"
_ERROR_SPA_SHELL_ONLY = "SPA_SHELL_ONLY"
_ERROR_TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
_ERROR_TARGET_MISMATCH = "TARGET_MISMATCH"
_ERROR_CLICK_NOT_VERIFIED = "CLICK_NOT_VERIFIED"
_ROUTING_HINT_KEYS = ("work_key", "target_url", "url_pattern", "expected_url")
DOWNLOAD_CONTENT_MAX_BYTES = max(
    0,
    int(os.getenv("AADS_BROWSER_DOWNLOAD_CONTENT_MAX_BYTES", str(2 * 1024 * 1024)) or "0"),
)
_MSG_ID = 0
_OWNED_TAB_LOCKS: dict[int, asyncio.Lock] = {}


def _owned_tab_lock(port: int) -> asyncio.Lock:
    """Serialize ownership-sensitive operations on one CDP port only."""
    return _OWNED_TAB_LOCKS.setdefault(port, asyncio.Lock())


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
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    connected_at: float = field(default_factory=_time)
    last_heartbeat_at: float = field(default_factory=_time)
    last_target_id: str = ""
    last_target_url: str = ""
    last_error_code: str = ""


@dataclass
class _CommandGuard:
    key: str
    owner: str
    acquired_at: float


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


class CDPCommandGuardManager:
    _guards: dict[str, _CommandGuard] = {}
    _lock = threading.Lock()

    @classmethod
    def _guard_key(cls, params: Dict[str, Any] | None, *, port: int) -> str:
        if isinstance(params, dict):
            work_key = str(params.get("work_key") or "").strip()
            if work_key:
                return f"work:{CDPSessionManager.normalize_work_key(work_key)}"
        return f"port:{port}"

    @classmethod
    async def acquire(
        cls,
        params: Dict[str, Any] | None,
        *,
        port: int,
        wait_timeout_seconds: float,
        stale_after_seconds: float,
    ) -> tuple[str, str, bool]:
        loop = asyncio.get_running_loop()
        key = cls._guard_key(params, port=port)
        owner = uuid.uuid4().hex
        reclaimed = False
        deadline = loop.time() + max(0.25, wait_timeout_seconds)
        while True:
            now = loop.time()
            with cls._lock:
                current = cls._guards.get(key)
                if current is None:
                    cls._guards[key] = _CommandGuard(key=key, owner=owner, acquired_at=now)
                    return key, owner, reclaimed
                age = max(0.0, now - current.acquired_at)
                if age >= stale_after_seconds:
                    cls._guards[key] = _CommandGuard(key=key, owner=owner, acquired_at=now)
                    reclaimed = True
                    logger.warning(
                        "cdp_command_guard_reclaimed key=%s previous_owner=%s age=%.2fs stale_after=%.2fs",
                        key,
                        current.owner,
                        age,
                        stale_after_seconds,
                    )
                    return key, owner, reclaimed
            if now >= deadline:
                raise CDPCommandError(
                    _ERROR_RUNTIME_EVALUATE_TIMEOUT,
                    f"previous command lock busy: {key}",
                    details={
                        "guard_key": key,
                        "guard_wait_seconds": max(0.0, wait_timeout_seconds),
                        "guard_stale_after_seconds": max(0.0, stale_after_seconds),
                    },
                )
            await asyncio.sleep(0.05)

    @classmethod
    def release(cls, key: str, owner: str) -> None:
        if not key or not owner:
            return
        with cls._lock:
            current = cls._guards.get(key)
            if current is not None and current.owner == owner:
                cls._guards.pop(key, None)

    @classmethod
    def force_release(cls, key: str) -> None:
        if not key:
            return
        with cls._lock:
            cls._guards.pop(key, None)


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
        install_dir = str(os.environ.get("KAKAOBOT_INSTALL_DIR") or "").strip()
        if install_dir:
            return os.path.join(install_dir, "cdp-profile")
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


def _coerce_window_pair(value: Any, *, first_key: str, second_key: str) -> tuple[int, int] | None:
    if value is None or value == "":
        return None
    raw_first: Any = None
    raw_second: Any = None
    if isinstance(value, dict):
        raw_first = value.get(first_key)
        raw_second = value.get(second_key)
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        raw_first, raw_second = value[0], value[1]
    else:
        parts = [part.strip() for part in str(value).replace("x", ",").split(",", 1)]
        if len(parts) == 2:
            raw_first, raw_second = parts
    try:
        first = int(float(str(raw_first).strip()))
        second = int(float(str(raw_second).strip()))
    except Exception:
        return None
    return first, second


def _browser_window_cli_args(params: Dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    args: list[str] = []
    metadata: dict[str, str] = {}
    position = _coerce_window_pair(params.get("window_position"), first_key="x", second_key="y")
    if position is None and ("window_x" in params or "window_y" in params):
        position = _coerce_window_pair(
            {"x": params.get("window_x"), "y": params.get("window_y")},
            first_key="x",
            second_key="y",
        )
    if position is not None:
        args.append(f"--window-position={position[0]},{position[1]}")
        metadata["window_position"] = f"{position[0]},{position[1]}"

    size = _coerce_window_pair(params.get("window_size"), first_key="width", second_key="height")
    if size is None and ("window_width" in params or "window_height" in params):
        size = _coerce_window_pair(
            {"width": params.get("window_width"), "height": params.get("window_height")},
            first_key="width",
            second_key="height",
        )
    if size is not None and size[0] >= 320 and size[1] >= 240:
        args.append(f"--window-size={size[0]},{size[1]}")
        metadata["window_size"] = f"{size[0]},{size[1]}"

    policy = str(params.get("window_layout_policy") or "").strip()
    if policy:
        metadata["window_layout_policy"] = policy[:80]
    return args, metadata


def _is_managed_profile_dir(profile_dir: str) -> bool:
    """Return true only for Chrome profiles created under the PC Agent profile root."""
    value = str(profile_dir or "").strip()
    if not value:
        return False
    try:
        root = os.path.abspath(os.path.normpath(_default_profile_root()))
        path = os.path.abspath(os.path.normpath(os.path.expandvars(os.path.expanduser(value))))
        return path == root or path.startswith(root + os.sep)
    except Exception:
        return False


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


def _url_hostname(value: str) -> str:
    try:
        return urllib.parse.urlparse(str(value or "")).hostname or ""
    except Exception:
        return ""


def _same_or_child_host(candidate: str, expected: str) -> bool:
    candidate_host = _url_hostname(candidate).lower()
    expected_host = _url_hostname(expected).lower()
    if not candidate_host or not expected_host:
        return False
    return candidate_host == expected_host or candidate_host.endswith("." + expected_host)


async def _bank_work_key_port_matches_url(port: int, requested_url: str) -> bool:
    requested = str(requested_url or "").strip()
    if not requested or requested == "about:blank":
        return False
    try:
        targets = await _list_cdp_targets(port)
    except Exception:
        return False
    for target in targets:
        if not isinstance(target, dict):
            continue
        if str(target.get("type") or "").strip().lower() not in {"", "page"}:
            continue
        if _same_or_child_host(str(target.get("url") or ""), requested):
            return True
    return False


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


def _target_matches_url_hint(target: dict[str, Any], *, target_url: str = "", url_pattern: str = "") -> bool:
    url = str(target.get("url") or "")
    title = str(target.get("title") or "")
    if target_url and _same_or_child_host(url, target_url):
        return True
    if url_pattern:
        try:
            return bool(re.search(url_pattern, f"{url} {title}", re.IGNORECASE))
        except re.error:
            return False
    return False


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
    target_url: str = "",
    url_pattern: str = "",
    require_url_match: bool = False,
    strict_target: bool = False,
    expected_url: str = "",
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
    if strict_target and target_id:
        # Explicit target: this tab or nothing. Falling back to another tab would
        # run a click/read against a page the caller never named.
        exact = [page for page in pages if str(page.get("targetId") or "") == target_id]
        if not exact:
            raise CDPCommandError(
                _ERROR_TARGET_NOT_FOUND,
                f"지정한 탭을 찾을 수 없습니다 (target_id={target_id}, port={port})",
                details={"port": port, "target_id": target_id, "open_page_count": len(pages)},
            )
        if expected_url and not _same_or_child_host(str(exact[0].get("url") or ""), expected_url):
            raise CDPCommandError(
                _ERROR_TARGET_MISMATCH,
                "지정한 탭의 도메인이 기대한 URL과 다릅니다",
                details={
                    "port": port,
                    "target_id": target_id,
                    "expected_host": _url_hostname(expected_url),
                    "actual_host": _url_hostname(str(exact[0].get("url") or "")),
                },
            )
        return exact[:1]
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
    elif target_url or url_pattern:
        matched = [
            page for page in pages
            if _target_matches_url_hint(page, target_url=target_url, url_pattern=url_pattern)
        ]
        if matched:
            pages = sorted(matched, key=lambda target: _target_sort_key(target)) + [
                page for page in pages if page not in matched
            ]
        elif require_url_match:
            raise CDPCommandError(
                _ERROR_STALE_TARGET,
                f"matching page target 없음 (port={port})",
                details={
                    "port": port,
                    "target_url": target_url,
                    "url_pattern": url_pattern,
                },
            )
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
            "navigated or closed",
            "inspected target navigated",
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

    async with await _connect_cdp_ws(
        websockets,
        ws_url,
        open_timeout=min(CDP_CONNECT_TIMEOUT_SECONDS, max(0.5, timeout_seconds)),
    ) as ws:
        return await _request_cdp(
            ws,
            method,
            params,
            timeout_seconds=timeout_seconds,
            session_id=session_id,
        )


async def _connect_cdp_ws(
    websockets: Any,
    ws_url: str,
    *,
    open_timeout: float,
) -> Any:
    last_exc: Exception | None = None
    attempts = max(1, CDP_WS_CONNECT_RETRY_LIMIT + 1)
    timeout = max(0.5, float(open_timeout or CDP_CONNECT_TIMEOUT_SECONDS))
    for attempt in range(attempts):
        try:
            return await websockets.connect(
                ws_url,
                open_timeout=timeout,
                close_timeout=1,
                max_size=10 * 1024 * 1024,
            )
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                break
            await asyncio.sleep(min(1.5, 0.35 * (attempt + 1)))
    raise CDPCommandError(
        _ERROR_CDP_NOT_READY,
        f"CDP websocket opening handshake failed after {attempts} attempts: {last_exc}",
        details={"ws_url": ws_url, "attempts": attempts, "open_timeout": timeout},
    )


async def _best_effort_runtime_terminate(
    ws: Any,
    *,
    session_id: str,
    timeout_seconds: float,
    reason: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "action": "Runtime.terminateExecution",
        "reason": reason,
        "attempted": bool(session_id),
        "succeeded": False,
    }
    if not session_id:
        return report
    try:
        await asyncio.shield(
            _request_cdp(
                ws,
                "Runtime.terminateExecution",
                None,
                timeout_seconds=max(0.5, timeout_seconds),
                session_id=session_id,
            )
        )
        report["succeeded"] = True
    except BaseException as exc:
        report["error"] = str(exc)
    return report


async def _best_effort_detach(
    ws: Any,
    *,
    session_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "action": "Target.detachFromTarget",
        "attempted": bool(session_id),
        "succeeded": False,
    }
    if not session_id:
        return report
    try:
        await asyncio.shield(
            _request_cdp(
                ws,
                "Target.detachFromTarget",
                {"sessionId": session_id},
                timeout_seconds=max(0.5, timeout_seconds),
            )
        )
        report["succeeded"] = True
    except BaseException as exc:
        report["error"] = str(exc)
    return report


async def _best_effort_target_probe(
    ws: Any,
    *,
    target_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "action": "Target.getTargetInfo",
        "target_id": target_id,
        "attempted": bool(target_id),
        "succeeded": False,
    }
    if not target_id:
        return report
    try:
        await _request_cdp(
            ws,
            "Target.getTargetInfo",
            {"targetId": target_id},
            timeout_seconds=max(0.5, timeout_seconds),
        )
        report["succeeded"] = True
    except Exception as exc:
        report["error"] = str(exc)
    return report


def _strip_routing_hints(params: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Routing hints steer tab selection; they are not CDP protocol fields."""
    if not isinstance(params, dict) or not any(key in params for key in _ROUTING_HINT_KEYS):
        return params
    return {key: value for key, value in params.items() if key not in _ROUTING_HINT_KEYS}


async def _send_cdp_command_unlocked(
    port: int,
    method: str,
    params: Dict[str, Any] | None = None,
    *,
    timeout_seconds: float,
    target_id: str = "",
    target_idx: int = 0,
    work_key: str = "",
    target_url: str = "",
    url_pattern: str = "",
    expected_url: str = "",
) -> Dict[str, Any]:
    import websockets

    browser_ws_url = await _get_browser_ws_url(port)
    if method.startswith("Browser.") or method.startswith("Target."):
        return await _send_cdp(browser_ws_url, method, params, timeout_seconds=timeout_seconds)

    hints = params or {}
    # Keyword routing wins; hints embedded in params stay supported for older callers.
    target_url_hint = str(target_url or hints.get("target_url") or hints.get("url") or "")
    url_pattern_hint = str(url_pattern or hints.get("url_pattern") or "")
    expected_url = str(expected_url or hints.get("expected_url") or "")
    work_key = CDPSessionManager.normalize_work_key(work_key or hints.get("work_key") or "general")
    params = _strip_routing_hints(params)
    require_url_match = bool((target_url_hint or url_pattern_hint) and work_key.startswith("yeoljeong-bank-"))

    strict_target = bool(target_id)
    selection = "explicit" if strict_target else ""
    if not target_id and not (target_url_hint or url_pattern_hint):
        session = CDPSessionManager.get_session(work_key)
        if session and session.last_target_id:
            target_id = session.last_target_id
            selection = "session"
    if not selection:
        selection = "hint" if (target_url_hint or url_pattern_hint) else "index"

    candidates = await _select_page_targets(
        port,
        target_id=target_id,
        target_idx=target_idx,
        target_url=target_url_hint,
        url_pattern=url_pattern_hint,
        require_url_match=require_url_match,
        strict_target=strict_target,
        expected_url=expected_url,
    )
    if strict_target:
        attempts = candidates[:1]
    else:
        attempts = candidates[: max(1, min(len(candidates), CDP_RECOVERY_RETRY_LIMIT + 1))]
    last_error: CDPCommandError | None = None
    deadline = asyncio.get_running_loop().time() + max(0.5, timeout_seconds)

    for idx, candidate in enumerate(attempts):
        remaining_budget = deadline - asyncio.get_running_loop().time()
        if remaining_budget <= 0:
            break
        per_attempt_timeout = max(
            0.5,
            min(
                remaining_budget,
                max(0.5, timeout_seconds / max(1, len(attempts) - idx)),
            ),
        )
        candidate_target_id = str(candidate.get("targetId") or candidate.get("id") or "")
        candidate_url = str(candidate.get("url") or "")
        candidate_title = str(candidate.get("title") or "")
        if candidate_target_id:
            # Selection is already inside the port lock. A command may affect
            # the page and then time out, so record use before sending it.
            from . import browser_reclaim
            browser_reclaim.mark_observed(candidate_target_id)
        attempt_started = _monotonic()
        try:
            async with await _connect_cdp_ws(
                websockets,
                browser_ws_url,
                open_timeout=max(0.5, min(CDP_CONNECT_TIMEOUT_SECONDS, per_attempt_timeout)),
            ) as ws:
                connected_at = _monotonic()
                try:
                    await _request_cdp(
                        ws,
                        "Target.activateTarget",
                        {"targetId": candidate_target_id},
                        timeout_seconds=max(0.5, min(3.0, per_attempt_timeout)),
                    )
                except CDPCommandError:
                    pass

                attach = await _request_cdp(
                    ws,
                    "Target.attachToTarget",
                    {"targetId": candidate_target_id, "flatten": True},
                    timeout_seconds=max(0.5, min(3.0, per_attempt_timeout)),
                )
                session_id = str(attach.get("sessionId") or "")
                if not session_id:
                    raise CDPCommandError(
                        _ERROR_STALE_TARGET,
                        f"target attach 실패: {candidate_target_id}",
                        details={"target_id": candidate_target_id, "url": candidate_url},
                    )
                probe = await _best_effort_target_probe(
                    ws,
                    target_id=candidate_target_id,
                    timeout_seconds=max(0.5, min(1.5, per_attempt_timeout)),
                )
                if not probe.get("succeeded"):
                    raise CDPCommandError(
                        _ERROR_STALE_TARGET,
                        f"target probe 실패: {candidate_target_id}",
                        details={
                            "target_id": candidate_target_id,
                            "target_probe": probe,
                        },
                    )
                command_error: CDPCommandError | None = None
                attached_at = _monotonic()
                try:
                    pre_runtime_cleanup: dict[str, Any] = {}
                    if method == "Runtime.evaluate":
                        pre_runtime_cleanup = await _best_effort_runtime_terminate(
                            ws,
                            session_id=session_id,
                            timeout_seconds=max(0.5, min(1.5, per_attempt_timeout)),
                            reason="pre_eval_cleanup",
                        )
                    result = await _request_cdp(
                        ws,
                        method,
                        params,
                        timeout_seconds=max(0.5, min(per_attempt_timeout, deadline - asyncio.get_running_loop().time())),
                        session_id=session_id,
                    )
                    if pre_runtime_cleanup:
                        result["_cleanup"] = {"pre_runtime_cleanup": pre_runtime_cleanup}
                    result["_target"] = {
                        "id": candidate_target_id,
                        "url": candidate_url,
                        "title": candidate_title,
                        "selection": selection,
                    }
                    finished_at = _monotonic()
                    result["_timing"] = {
                        "handshake_ms": round((connected_at - attempt_started) * 1000),
                        "attach_ms": round((attached_at - connected_at) * 1000),
                        "command_ms": round((finished_at - attached_at) * 1000),
                    }
                    return result
                except CDPCommandError as cmd_exc:
                    command_error = cmd_exc
                    if cmd_exc.code == _ERROR_RUNTIME_EVALUATE_TIMEOUT and method == "Runtime.evaluate":
                        cmd_exc.details["runtime_cleanup"] = await _best_effort_runtime_terminate(
                            ws,
                            session_id=session_id,
                            timeout_seconds=max(0.5, min(2.0, per_attempt_timeout)),
                            reason="timeout_cleanup",
                        )
                    if method == "Runtime.evaluate" and "runtime_cleanup" not in cmd_exc.details:
                        cmd_exc.details["runtime_cleanup"] = await _best_effort_runtime_terminate(
                            ws,
                            session_id=session_id,
                            timeout_seconds=max(0.5, min(1.5, per_attempt_timeout)),
                            reason="error_cleanup",
                        )
                    raise
                finally:
                    detach_cleanup = await _best_effort_detach(
                        ws,
                        session_id=session_id,
                        timeout_seconds=max(0.5, min(1.5, per_attempt_timeout)),
                    )
                    if command_error is not None:
                        command_error.details.setdefault("detach_cleanup", detach_cleanup)
        except CDPCommandError as exc:
            last_error = exc
            if exc.code not in {_ERROR_STALE_TARGET, _ERROR_RUNTIME_EVALUATE_TIMEOUT, _ERROR_CDP_NOT_READY}:
                raise
            if strict_target:
                raise
            if exc.code == _ERROR_STALE_TARGET and (deadline - asyncio.get_running_loop().time()) > 2.0:
                await asyncio.sleep(1.5)

    if last_error is not None:
        raise last_error
    if method == "Runtime.evaluate":
        raise CDPCommandError(
            _ERROR_RUNTIME_EVALUATE_TIMEOUT,
            f"{method} timed out after {timeout_seconds:.1f}s",
            details={"method": method, "timeout_seconds": timeout_seconds, "port": port},
        )
    raise CDPCommandError(
        _ERROR_STALE_TARGET,
        f"usable page target 없음 (port={port})",
        details={"port": port},
    )


async def _send_cdp_command(
    port: int,
    method: str,
    params: Dict[str, Any] | None = None,
    *,
    timeout_seconds: float,
    target_id: str = "",
    target_idx: int = 0,
    work_key: str = "",
    target_url: str = "",
    url_pattern: str = "",
    expected_url: str = "",
) -> Dict[str, Any]:
    # _send_cdp_command_unlocked calls _send_cdp/_request_cdp directly; it does
    # not call this wrapper. Reclaim uses those same low-level calls under this
    # lock, so its last check and close cannot race a normal command on the port.
    requested_at = _monotonic()
    async with _owned_tab_lock(port):
        lock_wait_ms = round((_monotonic() - requested_at) * 1000)
        routing: Dict[str, Any] = {}
        for key, value in (
            ("work_key", work_key), ("target_url", target_url),
            ("url_pattern", url_pattern), ("expected_url", expected_url),
        ):
            if value:
                routing[key] = value
        result = await _send_cdp_command_unlocked(
            port, method, params, timeout_seconds=timeout_seconds,
            target_id=target_id, target_idx=target_idx, **routing,
        )
        if isinstance(result, dict) and isinstance(result.get("_timing"), dict):
            result["_timing"]["queue_ms"] = lock_wait_ms
        target = result.get("_target") if isinstance(result, dict) else None
        if isinstance(target, dict):
            resolved_id = str(target.get("id") or target.get("targetId") or "")
            if resolved_id:
                from . import browser_reclaim
                browser_reclaim.mark_observed(resolved_id)
                session = CDPSessionManager.get_session(work_key) if work_key else None
                session = session or CDPSessionManager.get_by_port(port)
                if session:
                    CDPSessionManager.mark_healthy(
                        session.work_key, target_id=resolved_id,
                        target_url=str(target.get("url") or ""),
                    )
        return result


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


def _routing_kwargs(params: Dict[str, Any] | None) -> Dict[str, Any]:
    """Tab routing carried by a browser_* command; empty values are omitted."""
    source = params if isinstance(params, dict) else {}
    routing: Dict[str, Any] = {}
    for key in ("target_id", "work_key", "target_url", "url_pattern", "expected_url"):
        value = str(source.get(key) or "").strip()
        if value:
            routing[key] = value
    try:
        target_idx = int(source.get("target_idx") or 0)
    except (TypeError, ValueError):
        target_idx = 0
    if target_idx:
        routing["target_idx"] = target_idx
    return routing


def _attach_target(data: Any, result: Dict[str, Any] | None) -> Any:
    """Report which tab actually answered so callers can verify pinning."""
    target = result.get("_target") if isinstance(result, dict) else None
    if isinstance(data, dict) and isinstance(target, dict):
        if target.get("id"):
            data.setdefault("target_id", target.get("id"))
        if target.get("url"):
            data.setdefault("target_url", target.get("url"))
        if target.get("selection"):
            data.setdefault("target_selection", target.get("selection"))
    return data


def _target_error_details(exc: CDPCommandError) -> CDPCommandError:
    if exc.code in {_ERROR_TARGET_NOT_FOUND, _ERROR_TARGET_MISMATCH}:
        exc.details.setdefault("dispatch_state", "not_dispatched")
    else:
        exc.details.setdefault("dispatch_state", "uncertain")
    exc.details.setdefault("retry_safe", False)
    return exc


def _pin_routing(routing: Dict[str, Any], result: Dict[str, Any] | None) -> Dict[str, Any]:
    """Follow-up CDP calls of one command must hit the tab that answered the first call."""
    target = result.get("_target") if isinstance(result, dict) else None
    pinned = dict(routing)
    if isinstance(target, dict) and target.get("id"):
        pinned["target_id"] = str(target["id"])
        pinned.pop("target_idx", None)
    return pinned


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
    release_session = exc.code in {_ERROR_CDP_NOT_READY, _ERROR_STALE_TARGET}
    if exc.code == _ERROR_RUNTIME_EVALUATE_TIMEOUT:
        runtime_cleanup = exc.details.get("runtime_cleanup") if isinstance(exc.details, dict) else None
        detach_cleanup = exc.details.get("detach_cleanup") if isinstance(exc.details, dict) else None
        runtime_ok = bool(isinstance(runtime_cleanup, dict) and runtime_cleanup.get("succeeded"))
        detach_ok = bool(isinstance(detach_cleanup, dict) and detach_cleanup.get("succeeded"))
        release_session = not (runtime_ok and detach_ok)
    if release_session:
        CDPSessionManager.release(work_key)
    data = {"error": str(exc), "error_code": exc.code, "port": port}
    data.update(exc.details)
    data.setdefault("work_key", work_key)
    data["session_released"] = release_session
    return {"status": "error", "data": data}


async def _diagnose_cdp_failure(
    port: int,
    params: Dict[str, Any] | None,
    exc: CDPCommandError,
) -> CDPCommandError:
    if exc.code not in {_ERROR_CDP_NOT_READY, _ERROR_RUNTIME_EVALUATE_TIMEOUT, _ERROR_STALE_TARGET}:
        return exc
    if exc.code == _ERROR_RUNTIME_EVALUATE_TIMEOUT and _as_bool((params or {}).get("skip_timeout_diagnostics", True), default=True):
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
    """URL 이동. params: url(필수), reuse_tab(선택, 기본true — 동일 도메인 탭 재사용)"""
    url = params.get("url", "")
    if not url:
        return {"status": "error", "data": {"error": "url 파라미터가 필요합니다"}}

    port = _effective_port(params)
    reuse_tab = _as_bool(params.get("reuse_tab", True), default=True)
    target_id = str(params.get("target_id", "") or "")
    explicit_target = bool(target_id)
    reused = False

    if not target_id:
        session = CDPSessionManager.get_session(_work_key_from_params(params))
        if session and session.last_target_id:
            target_id = session.last_target_id

    if reuse_tab and not target_id:
        try:
            from urllib.parse import urlparse
            target_domain = urlparse(url).netloc.lower()
            if target_domain:
                existing_targets = await _list_cdp_targets(port)
                for t in existing_targets:
                    if str(t.get("type") or "").lower() != "page":
                        continue
                    existing_url = str(t.get("url") or "")
                    existing_domain = urlparse(existing_url).netloc.lower()
                    if existing_domain == target_domain:
                        target_id = str(t.get("id") or t.get("targetId") or "")
                        reused = True
                        logger.info("탭 재사용: domain=%s target_id=%s", target_domain, target_id)
                        break
        except Exception:
            pass

    try:
        timeout_seconds = _resolve_timeout(params, param_name="page_timeout_seconds", default=CDP_COMMAND_TIMEOUT_SECONDS, maximum=90.0)
        try:
            result = await _send_cdp_command(port, "Page.navigate", {"url": url}, timeout_seconds=timeout_seconds, target_id=target_id)
        except CDPCommandError as nav_exc:
            # A session/domain-derived tab may have been closed; only a caller-supplied target is binding.
            if explicit_target or not target_id or nav_exc.code != _ERROR_TARGET_NOT_FOUND:
                raise
            target_id = ""
            reused = False
            result = await _send_cdp_command(port, "Page.navigate", {"url": url}, timeout_seconds=timeout_seconds, target_id="")
        target = result.get("_target", {}) if isinstance(result, dict) else {}
        navigated_target_id = str(target.get("id") or target.get("targetId") or target_id or "")
        CDPSessionManager.mark_healthy(
            _work_key_from_params(params),
            target_id=navigated_target_id,
            target_url=str(url),
        )
        logger.info("브라우저 이동: %s (reused=%s)", url, reused)
        return {
            "status": "success",
            "data": {
                "url": url,
                "frameId": result.get("frameId", ""),
                "target_id": navigated_target_id,
                "target_url": url,
                "reused_tab": reused,
            },
        }
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, _target_error_details(exc))
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


_STATE_FINGERPRINT_JS_FN = r"""
function __fp() {
  var body = document.body;
  var text = body ? String(body.innerText || '') : '';
  var h = 5381, n = Math.min(text.length, 20000);
  for (var i = 0; i < n; i++) { h = ((h << 5) + h + text.charCodeAt(i)) | 0; }
  var states = '';
  var nodes = document.querySelectorAll('input[type=checkbox],input[type=radio],[aria-checked],[aria-expanded],[aria-pressed],[aria-selected],details,option');
  for (var j = 0; j < nodes.length && j < 400; j++) {
    var e = nodes[j];
    states += (e.checked ? 'c' : '') + (e.selected ? 's' : '') + (e.open ? 'o' : '') + '|' +
      (e.getAttribute('aria-checked') || '') + (e.getAttribute('aria-expanded') || '') +
      (e.getAttribute('aria-pressed') || '') + (e.getAttribute('aria-selected') || '') + ';';
  }
  var g = 5381;
  for (var k = 0; k < states.length; k++) { g = ((g << 5) + g + states.charCodeAt(k)) | 0; }
  var dialogs = 0;
  var dl = document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog],[aria-modal=true]');
  for (var d = 0; d < dl.length; d++) {
    var rr = dl[d].getBoundingClientRect();
    if (rr.width > 0 && rr.height > 0) dialogs++;
  }
  return {href: String(location.href || ''), ready: document.readyState || '', textLen: text.length,
          textHash: h, stateHash: g, dialogs: dialogs};
}
"""

_CLICK_RESOLVE_JS = r"""
(function() {
__FP_FN__
var spec = __SPEC__;
function norm(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
function visible(el) {
  var r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  var cs = getComputedStyle(el);
  return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0;
}
function nameOf(el) {
  var t = (el.tagName || '').toLowerCase();
  var ty = (el.getAttribute('type') || '').toLowerCase();
  if (t === 'input' && (ty === 'button' || ty === 'submit' || ty === 'reset')) return norm(el.value);
  return norm(el.getAttribute('aria-label') || el.innerText || el.textContent || el.getAttribute('title'));
}
function describe(el) {
  return {tag: (el.tagName || '').toLowerCase(), role: el.getAttribute('role') || '',
          type: (el.getAttribute('type') || '').toLowerCase(),
          id: el.id ? String(el.id).slice(0, 40) : '', text: nameOf(el).slice(0, 40)};
}
function fail(code, message, extra) {
  var o = {error: message, error_code: code, dispatched: false};
  if (extra) { for (var k in extra) o[k] = extra[k]; }
  return JSON.stringify(o);
}
var ACT = 'a[href],button,input:not([type=hidden]),select,textarea,summary,label,[role=button],[role=link],[role=menuitem],[role=tab],[role=checkbox],[role=radio],[role=switch],[role=option],[onclick],[tabindex]:not([tabindex="-1"])';
var matches = [];
try {
  var tm = /^text\s*=\s*([\s\S]*)$/i.exec(spec.selector);
  if (tm) {
    var want = tm[1].trim(), exact = false;
    var q = /^(["'])([\s\S]*)\1$/.exec(want);
    if (q) { exact = true; want = q[2]; }
    want = norm(want);
    var lw = want.toLowerCase();
    var hits = Array.prototype.slice.call(document.querySelectorAll(ACT)).filter(function(el) {
      var n = nameOf(el);
      return exact ? n === want : n.toLowerCase().indexOf(lw) >= 0;
    });
    matches = hits.filter(function(c) { return !hits.some(function(o) { return o !== c && c.contains(o); }); });
    if (!matches.length && want) {
      var loose = 0, all = document.body ? document.body.querySelectorAll('*') : [];
      for (var i = 0; i < all.length && i < 6000; i++) {
        var n2 = norm(all[i].innerText || '');
        if (n2 && (exact ? n2 === want : n2.toLowerCase().indexOf(lw) >= 0) && visible(all[i])) loose++;
      }
      if (loose) return fail('ELEMENT_NOT_ACTIONABLE',
        'text 와 일치하는 텍스트는 있으나 클릭 가능한 요소(버튼/링크 등)가 아닙니다', {matched_text_elements: loose});
    }
  } else {
    matches = Array.prototype.slice.call(document.querySelectorAll(spec.selector));
  }
} catch (err) {
  return fail('INVALID_SELECTOR', 'selector 해석 실패: ' + String((err && err.message) || err).slice(0, 120));
}
if (!matches.length) return fail('ELEMENT_NOT_FOUND', '요소를 찾을 수 없습니다: ' + spec.selector, {matched_count: 0});
var vis = matches.filter(visible);
if (!vis.length) return fail('ELEMENT_HIDDEN', '일치하는 요소가 모두 보이지 않습니다: ' + spec.selector, {matched_count: matches.length, visible_count: 0});
var el;
if (spec.nth !== null) {
  if (spec.nth < 0 || spec.nth >= vis.length) return fail('NTH_OUT_OF_RANGE', 'nth 범위 초과', {visible_count: vis.length});
  el = vis[spec.nth];
} else if (vis.length > 1 && spec.strict) {
  return fail('SELECTOR_AMBIGUOUS', '일치하는 보이는 요소가 ' + vis.length + '개입니다. selector 를 좁히거나 nth 를 지정하세요',
    {matched_count: matches.length, visible_count: vis.length, candidates: vis.slice(0, 5).map(describe)});
} else {
  el = vis[0];
}
if (el.disabled || el.getAttribute('aria-disabled') === 'true' || (el.closest && el.closest('fieldset:disabled'))) {
  return fail('ELEMENT_DISABLED', '요소가 비활성(disabled) 상태입니다', {target: describe(el)});
}
el.scrollIntoView({block: 'center', inline: 'center'});
var r = el.getBoundingClientRect();
var top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
if (top && top !== el && !el.contains(top) && !top.contains(el)) {
  var lab = top.closest ? top.closest('label') : null;
  var paired = lab && (lab === el || (el.labels && Array.prototype.indexOf.call(el.labels, lab) >= 0));
  if (!paired) return fail('ELEMENT_OBSCURED', '다른 요소가 클릭 지점을 가리고 있습니다', {target: describe(el), covering: describe(top)});
}
var before = __fp();
var hasChecked = 'checked' in el;
var checkedBefore = hasChecked ? !!el.checked : null;
el.click();
return JSON.stringify({dispatched: true, before: before, target: describe(el),
  matched_count: matches.length, visible_count: vis.length,
  checked_before: checkedBefore, checked_after: hasChecked ? !!el.checked : null});
})()
"""

_EXPECT_KEYS = {
    "url_contains", "url_changed", "text_present", "text_absent",
    "selector_visible", "selector_absent", "checked",
}
_EXPECT_EVAL_JS = r"""
(function() {
var spec = __SPEC__;
var body = document.body ? String(document.body.innerText || '') : '';
function visible(el) {
  var r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  var cs = getComputedStyle(el);
  return cs.visibility !== 'hidden' && cs.display !== 'none';
}
var out = {href: String(location.href || '')};
if (spec.url_contains) out.url_contains = out.href.indexOf(spec.url_contains) >= 0;
if (spec.text_present) out.text_present = body.indexOf(spec.text_present) >= 0;
if (spec.text_absent) out.text_absent = body.indexOf(spec.text_absent) < 0;
if (spec.selector_visible) {
  try { out.selector_visible = Array.prototype.slice.call(document.querySelectorAll(spec.selector_visible)).some(visible); }
  catch (e) { out.selector_visible = false; }
}
if (spec.selector_absent) {
  try { out.selector_absent = document.querySelectorAll(spec.selector_absent).length === 0; }
  catch (e) { out.selector_absent = false; }
}
if (spec.checked && spec.checked.selector) {
  try {
    var ce = document.querySelector(spec.checked.selector);
    out.checked = !!ce && (!!ce.checked === !!spec.checked.value);
  } catch (e) { out.checked = false; }
}
return JSON.stringify(out);
})()
"""


def _normalize_expectation(raw: Any) -> tuple[Dict[str, Any], str]:
    """Return (expectation, error). Unknown keys are rejected rather than ignored."""
    if raw in (None, "", {}):
        return {}, ""
    if not isinstance(raw, dict):
        return {}, "expect 는 객체여야 합니다"
    unknown = sorted(set(raw) - _EXPECT_KEYS)
    if unknown:
        return {}, f"지원하지 않는 expect 키: {', '.join(unknown)}"
    spec: Dict[str, Any] = {}
    for key in ("url_contains", "text_present", "text_absent", "selector_visible", "selector_absent"):
        value = raw.get(key)
        if value not in (None, ""):
            spec[key] = str(value)[:300]
    if _as_bool(raw.get("url_changed", False)):
        spec["url_changed"] = True
    checked = raw.get("checked")
    if isinstance(checked, dict) and checked.get("selector"):
        spec["checked"] = {"selector": str(checked["selector"])[:300], "value": _as_bool(checked.get("value", True), default=True)}
    elif checked not in (None, ""):
        return {}, "expect.checked 는 {selector, value} 형식이어야 합니다"
    return spec, ""


def _click_state_changes(before: Dict[str, Any], after: Dict[str, Any]) -> list[str]:
    changed = []
    if before.get("href") != after.get("href"):
        changed.append("url")
    if before.get("textHash") != after.get("textHash") or before.get("textLen") != after.get("textLen"):
        changed.append("text")
    if before.get("stateHash") != after.get("stateHash"):
        changed.append("control_state")
    if before.get("dialogs") != after.get("dialogs"):
        changed.append("dialog")
    return changed


async def _read_json_eval(
    port: int, expression: str, *, timeout_seconds: float, attempts: int, routing: Dict[str, Any],
) -> tuple[Dict[str, Any] | None, str]:
    """Evaluate on the pinned tab; a navigating document gets a few bounded re-reads."""
    last = ""
    for attempt in range(max(1, attempts)):
        try:
            result = await _send_cdp_command(
                port, "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": False},
                timeout_seconds=timeout_seconds, **routing,
            )
            if _runtime_exception_error(result) is None:
                value = _decode_runtime_value(result)
                if isinstance(value, dict):
                    return value, ""
                last = "non_object_result"
            else:
                last = "runtime_exception"
        except CDPCommandError as exc:
            last = exc.code
            if exc.code in {_ERROR_TARGET_NOT_FOUND, _ERROR_TARGET_MISMATCH}:
                break
        if attempt + 1 < attempts:
            await asyncio.sleep(0.3)
    return None, last


async def browser_click(params: Dict[str, Any]) -> Dict[str, Any]:
    """요소 클릭. params: selector(필수; CSS 또는 text=…), nth, strict(기본 true), expect, verify_settle_ms.

    dispatched(클릭 이벤트 전달)와 verified(기대 후조건 확인)를 구분해 반환한다.
    재전송하지 않는다 — 효과가 불확실한 클릭을 다시 누르면 외부 쓰기가 중복될 수 있다.
    """
    selector = str(params.get("selector", "") or "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}
    expectation, expect_error = _normalize_expectation(params.get("expect"))
    if expect_error:
        return {"status": "error", "data": {"error": expect_error, "error_code": "INVALID_EXPECT", "dispatched": False}}
    nth_raw = params.get("nth")
    try:
        nth = int(nth_raw) if nth_raw not in (None, "") else None
    except (TypeError, ValueError):
        return {"status": "error", "data": {"error": "nth 는 정수여야 합니다", "error_code": "INVALID_NTH", "dispatched": False}}

    port = _effective_port(params)
    try:
        spec = {"selector": selector, "nth": nth, "strict": _as_bool(params.get("strict", True), default=True)}
        js = _CLICK_RESOLVE_JS.replace("__FP_FN__", _STATE_FINGERPRINT_JS_FN).replace("__SPEC__", json.dumps(spec))
        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=90.0)
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
            **_routing_kwargs(params),
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        data = _decode_runtime_value(result)
        if not isinstance(data, dict):
            return {"status": "error", "data": {"error": "클릭 결과를 해석하지 못했습니다", "error_code": "CLICK_RESULT_UNREADABLE", "dispatch_state": "uncertain", "retry_safe": False}}
        _attach_target(data, result)
        if data.get("error"):
            return {"status": "error", "data": data}
        _record_cdp_success(params, result.get("_target"))

        # Follow-ups must land on the tab that took the click, whatever selected it.
        pinned = dict(_routing_kwargs(params))
        resolved_id = str((result.get("_target") or {}).get("id") or "")
        if resolved_id:
            pinned["target_id"] = resolved_id
        pinned.pop("target_idx", None)

        before = data.pop("before", {}) if isinstance(data.get("before"), dict) else {}
        verify_started = _monotonic()
        checks: Dict[str, Any] = {}
        after: Dict[str, Any] | None = None
        verified = False
        if expectation:
            expect_timeout = _resolve_timeout(params, param_name="expect_timeout_seconds", default=4.0, maximum=20.0)
            deadline = _monotonic() + expect_timeout
            while True:
                observed, read_error = await _read_json_eval(
                    port, _EXPECT_EVAL_JS.replace("__SPEC__", json.dumps(expectation)),
                    timeout_seconds=5.0, attempts=1, routing=pinned,
                )
                if observed is not None:
                    after = {"href": observed.get("href", "")}
                    checks = {key: bool(observed.get(key)) for key in expectation if key in observed and key != "url_changed"}
                    if expectation.get("url_changed"):
                        checks["url_changed"] = str(observed.get("href", "")) != str(before.get("href", ""))
                    verified = bool(checks) and all(checks.values()) and set(checks) == set(expectation)
                    if verified:
                        break
                elif read_error in {_ERROR_TARGET_NOT_FOUND, _ERROR_TARGET_MISMATCH}:
                    checks["target"] = False
                    break
                if _monotonic() >= deadline:
                    break
                await asyncio.sleep(0.25)
            state = "verified" if verified else "failed"
            changed: list[str] = []
        else:
            try:
                settle_ms = float(params.get("verify_settle_ms", 700) or 700)
            except (TypeError, ValueError):
                settle_ms = 700.0
            await asyncio.sleep(max(0.0, min(settle_ms, 3000.0)) / 1000.0)
            observed, _ = await _read_json_eval(
                port, f"(function(){{{_STATE_FINGERPRINT_JS_FN} return JSON.stringify(__fp());}})()",
                timeout_seconds=5.0, attempts=3, routing=pinned,
            )
            if observed is None or not before:
                changed, state = [], "unknown"
            else:
                after = observed
                changed = _click_state_changes(before, observed)
                if data.get("checked_before") is not None and data.get("checked_before") != data.get("checked_after"):
                    changed.append("checked")
                state = "observed_change" if changed else "no_observed_effect"

        verify_ms = round((_monotonic() - verify_started) * 1000)
        href_after = str((after or {}).get("href") or "")
        data.update({
            "dispatched": True,
            "verified": verified,
            "verification_state": state,
            "retry_safe": False,
            "href_before": str(before.get("href") or ""),
            "href_after": href_after,
        })
        if href_after:
            data["target_url"] = href_after
        if checks:
            data["checks"] = checks
        if changed:
            data["changed"] = changed
        timing = dict(result.get("_timing") or {})
        timing["verify_ms"] = verify_ms
        data["timings_ms"] = timing
        logger.info("브라우저 클릭: %s state=%s", selector, state)
        if expectation and not verified:
            data.update({
                "error": "클릭은 전달됐지만 기대한 후조건을 확인하지 못했습니다",
                "error_code": _ERROR_CLICK_NOT_VERIFIED,
            })
            return {"status": "error", "data": data}
        return {"status": "success", "data": data}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, _target_error_details(exc))
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


async def _evaluate_dom_action(
    port: int,
    params: Dict[str, Any],
    js: str,
    *,
    log_message: tuple | None = None,
) -> Dict[str, Any]:
    """DOM 조작 JS 를 같은 target 에서 실행하고 결과 계약을 맞춘다.

    browser_fill / browser_select_option / browser_check 가 공유한다. target_id·work_key·
    target_url 은 _routing_kwargs 로 그대로 전달하므로 다른 탭 fallback 이 생기지 않는다.
    """
    timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=90.0)
    try:
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
            **_routing_kwargs(params),
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, _target_error_details(exc))
        return _command_error_response(port, params, exc)
    data = _decode_runtime_value(result)
    if isinstance(data, dict) and data.get("error"):
        return {"status": "error", "data": data}
    _record_cdp_success(params, result.get("_target"))
    if log_message:
        logger.info(*log_message)
    _attach_target(data, result)
    return {"status": "success", "data": data}


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
        return await _evaluate_dom_action(port, params, js, log_message=("브라우저 입력: %s", selector))
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
        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=90.0)
        target_info: dict[str, Any] | None = None
        routing = _routing_kwargs(params)
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
                **routing,
            )
            error = _runtime_exception_error(focus)
            if error is not None:
                raise error
            data = _decode_runtime_value(focus)
            if isinstance(data, dict) and data.get("error"):
                return {"status": "error", "data": data}
            target_info = focus.get("_target") if isinstance(focus.get("_target"), dict) else None
            routing = _pin_routing(routing, focus)

        if len(key) == 1:
            result = await _send_cdp_command(port, "Input.insertText", {"text": key}, timeout_seconds=timeout_seconds, **routing)
        else:
            result = await _send_cdp_command(port, "Input.dispatchKeyEvent", {"type": "keyDown", "key": key}, timeout_seconds=timeout_seconds, **routing)
            await _send_cdp_command(port, "Input.dispatchKeyEvent", {"type": "keyUp", "key": key}, timeout_seconds=timeout_seconds, **routing)
        _record_cdp_success(params, target_info or result.get("_target"))
        return {"status": "success", "data": _attach_target({"key": key, "selector": selector}, result)}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, _target_error_details(exc))
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
        return await _evaluate_dom_action(port, params, js)
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
        return await _evaluate_dom_action(port, params, js)
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
        routing = _routing_kwargs(params)
        doc = await _send_cdp_command(
            port,
            "DOM.getDocument",
            {"depth": -1, "pierce": True},
            timeout_seconds=timeout_seconds,
            **routing,
        )
        routing = _pin_routing(routing, doc)
        root_id = doc.get("root", {}).get("nodeId")
        node = await _send_cdp_command(
            port,
            "DOM.querySelector",
            {"nodeId": root_id, "selector": selector},
            timeout_seconds=timeout_seconds,
            **routing,
        )
        node_id = node.get("nodeId")
        if not node_id:
            return {"status": "error", "data": {"error": f"요소를 찾을 수 없습니다: {selector}"}}
        await _send_cdp_command(
            port,
            "DOM.setFileInputFiles",
            {"nodeId": node_id, "files": file_paths},
            timeout_seconds=timeout_seconds,
            **routing,
        )
        _record_cdp_success(params, node.get("_target") or doc.get("_target"))
        return {"status": "success", "data": _attach_target({"selector": selector, "files": file_paths, "count": len(file_paths)}, node)}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, _target_error_details(exc))
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
                size = os.path.getsize(path)
                data: Dict[str, Any] = {
                    "path": path,
                    "filename": os.path.basename(path),
                    "size": size,
                    "download_dir": download_dir,
                }
                if DOWNLOAD_CONTENT_MAX_BYTES and size <= DOWNLOAD_CONTENT_MAX_BYTES:
                    with open(path, "rb") as f:
                        data["content_base64"] = base64.b64encode(f.read()).decode("ascii")
                return {"status": "success", "data": data}
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
        result = await _send_cdp_command(port, "Page.captureScreenshot", cdp_params, timeout_seconds=timeout_seconds, **_routing_kwargs(params))
        img_data = result.get("data", "")
        _record_cdp_success(params, result.get("_target"))
        logger.info("브라우저 스크린샷 캡처 (%s)", fmt)
        return {
            "status": "success",
            "data": _attach_target(
                {"screenshot_base64": img_data, "format": fmt, "timings_ms": dict(result.get("_timing") or {})},
                result,
            ),
        }
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, _target_error_details(exc))
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

        timeout_seconds = _resolve_timeout(params, param_name="evaluate_timeout_seconds", default=CDP_EVALUATE_TIMEOUT_SECONDS, maximum=90.0)
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": False},
            timeout_seconds=timeout_seconds,
            **_routing_kwargs(params),
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        data = _decode_runtime_value(result)
        if isinstance(data, dict) and data.get("error"):
            return {"status": "error", "data": data}
        _record_cdp_success(params, result.get("_target"))
        target_info = result.get("_target") or {}
        diagnostics: dict[str, Any] = {}
        diagnostics_ms = 0
        if _looks_like_vvic_target(str(target_info.get("url") or ""), str(target_info.get("title") or "")):
            diagnostics_started = _monotonic()
            diagnostics = await _collect_page_diagnostics(
                port,
                timeout_seconds=min(3.0, timeout_seconds),
                target_id=str(target_info.get("id") or ""),
            )
            diagnostics_ms = round((_monotonic() - diagnostics_started) * 1000)
        if _is_spa_shell_only(diagnostics):
            spa_error = CDPCommandError(
                _ERROR_SPA_SHELL_ONLY,
                "VVIC 검색 페이지가 SPA 셸만 반환했습니다.",
                details={"diagnostics": diagnostics},
            )
            return _command_error_response(port, params, spa_error)
        if isinstance(data, dict):
            _attach_target(data, result)
            data["timings_ms"] = {**(result.get("_timing") or {}), "diagnostics_ms": diagnostics_ms}
        return {"status": "success", "data": data}
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, _target_error_details(exc))
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
    guard_key = ""
    guard_owner = ""
    guard_reclaimed = False
    try:
        timeout_seconds = _resolve_timeout(
            params,
            param_name="evaluate_timeout_seconds",
            default=CDP_EVALUATE_TIMEOUT_SECONDS,
            maximum=90.0,
        )
        timeout_seconds = min(timeout_seconds, max(1.0, command_budget_seconds - 0.5))
        await_promise = _as_bool(params.get("await_promise", False), default=False)
        guard_wait_seconds = min(CDP_COMMAND_GUARD_WAIT_SECONDS, max(0.5, command_budget_seconds - 0.25))
        guard_stale_after_seconds = max(CDP_COMMAND_GUARD_STALE_SECONDS, timeout_seconds + 2.0)
        guard_key, guard_owner, guard_reclaimed = await CDPCommandGuardManager.acquire(
            params,
            port=port,
            wait_timeout_seconds=guard_wait_seconds,
            stale_after_seconds=guard_stale_after_seconds,
        )
        cdp_params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }
        routing = _routing_kwargs(params)
        if params.get("url") and "target_url" not in routing:
            routing["target_url"] = str(params.get("url"))
        result = await _send_cdp_command(
            port,
            "Runtime.evaluate",
            cdp_params,
            timeout_seconds=timeout_seconds,
            **routing,
        )
        error = _runtime_exception_error(result)
        if error is not None:
            raise error
        res_data = result.get("result", {})
        if res_data.get("subtype") == "error":
            raise CDPCommandError(_ERROR_CDP_NOT_READY, str(res_data.get("description", "JS 실행 오류")))
        _record_cdp_success(params, result.get("_target"))
        diagnostics: dict[str, Any] = {}
        diagnostics_ms = 0
        target = result.get("_target", {}) if isinstance(result, dict) else {}
        remaining_budget = command_budget_seconds - max(0.0, _time() - started_at)
        if remaining_budget > 0.7 and _looks_like_vvic_target(str(target.get("url") or ""), str(target.get("title") or "")):
            diagnostics_started = _monotonic()
            diagnostics = await _collect_page_diagnostics(
                port,
                timeout_seconds=min(3.0, timeout_seconds, max(0.5, remaining_budget - 0.25)),
                target_id=str(target.get("id") or ""),
            )
            diagnostics_ms = round((_monotonic() - diagnostics_started) * 1000)
        if _is_spa_shell_only(diagnostics):
            spa_error = CDPCommandError(
                _ERROR_SPA_SHELL_ONLY,
                "VVIC 검색 페이지가 SPA 셸만 반환했습니다.",
                details={"diagnostics": diagnostics},
            )
            return _command_error_response(port, params, spa_error)
        timings = dict(result.get("_timing") or {})
        timings["diagnostics_ms"] = diagnostics_ms
        return {
            "status": "success",
            "data": {
                "value": res_data.get("value"),
                "type": res_data.get("type", ""),
                "target_id": target.get("id"),
                "target_url": target.get("url"),
                "target_selection": target.get("selection"),
                "timings_ms": timings,
                "diagnostics": diagnostics,
                "await_promise": await_promise,
                "guard_reclaimed": guard_reclaimed,
                "cleanup": result.get("_cleanup", {}),
            },
        }
    except CDPCommandError as exc:
        exc = await _diagnose_cdp_failure(port, params, _target_error_details(exc))
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}
    finally:
        CDPCommandGuardManager.release(guard_key, guard_owner)


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
    guard_key = CDPCommandGuardManager._guard_key({"work_key": work_key}, port=port)
    guard_released = False

    session = CDPSessionManager.get_session(work_key)
    if session:
        port = session.port
        guard_key = CDPCommandGuardManager._guard_key({"work_key": work_key}, port=port)

    if do_cleanup:
        CDPCommandGuardManager.force_release(guard_key)
        guard_released = True

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
                "guard_released": guard_released,
            },
        }

    try:
        result = await _send_cdp_command(port, "Runtime.evaluate", {
            "expression": "JSON.stringify({readyState: document.readyState, href: location.href, bodyLen: document.body ? document.body.innerText.length : -1})",
            "returnByValue": True,
            "awaitPromise": False,
        }, timeout_seconds=5, work_key=work_key)
        value = result.get("result", {}).get("value", "{}")
        data = json.loads(value) if isinstance(value, str) else (value or {})
        return {
            "status": "success",
            "data": {
                "port": port,
                "work_key": work_key,
                "cdp_version": version.get("Browser", ""),
                "page": data,
                "guard_released": guard_released,
                "cleanup": result.get("_cleanup", {}),
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
                "guard_released": guard_released,
            },
        }
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY}}


async def browser_close_tab(params: Dict[str, Any]) -> Dict[str, Any]:
    """탭 닫기. params: target_id(선택), url_pattern(선택), close_others(선택, 현재 탭 외 모두 닫기)."""
    import re as _re

    port = _effective_port(params)
    target_id = str(params.get("target_id", "") or "").strip()
    url_pattern = str(params.get("url_pattern", "") or "").strip()
    close_others = _as_bool(params.get("close_others", False), default=False)
    keep_last = _as_bool(params.get("keep_last", True), default=True)

    try:
        targets = await _list_cdp_targets(port)
        pages = [
            t for t in targets
            if str(t.get("type") or "").lower() == "page" and str(t.get("webSocketDebuggerUrl") or "")
        ]
        if not pages:
            return {"status": "success", "data": {"closed": 0, "remaining": 0, "message": "닫을 탭 없음"}}

        to_close: list[dict[str, Any]] = []
        if target_id:
            to_close = [
                t for t in pages
                if str(t.get("id") or t.get("targetId") or "") == target_id
            ]
        elif url_pattern:
            try:
                compiled = _re.compile(url_pattern, _re.IGNORECASE)
                to_close = [t for t in pages if compiled.search(str(t.get("url") or ""))]
            except _re.error:
                return {"status": "error", "data": {"error": f"잘못된 정규식: {url_pattern}"}}
        elif close_others:
            sorted_pages = sorted(pages, key=lambda t: _target_sort_key(t))
            if len(sorted_pages) > 1:
                to_close = sorted_pages[1:]

        if not to_close:
            return {"status": "success", "data": {"closed": 0, "remaining": len(pages), "message": "닫을 대상 없음"}}

        if keep_last and len(to_close) >= len(pages):
            to_close = to_close[:-1]

        closed: list[dict[str, Any]] = []
        browser_ws_url = await _get_browser_ws_url(port)
        for tab in to_close:
            tid = str(tab.get("id") or tab.get("targetId") or "")
            if not tid:
                continue
            try:
                await _send_cdp(
                    browser_ws_url,
                    "Target.closeTarget",
                    {"targetId": tid},
                    timeout_seconds=5,
                )
                closed.append({"id": tid, "url": str(tab.get("url") or ""), "title": str(tab.get("title") or "")})
                logger.info("탭 닫기 완료: %s (%s)", tid, tab.get("url", ""))
            except Exception as exc:
                logger.warning("탭 닫기 실패: %s — %s", tid, exc)

        return {
            "status": "success",
            "data": {
                "closed": len(closed),
                "closed_tabs": closed,
                "remaining": len(pages) - len(closed),
            },
        }
    except CDPCommandError as exc:
        return _command_error_response(port, params, exc)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": _ERROR_CDP_NOT_READY, "port": port}}


def _terminate_browser_process(pid: int) -> dict[str, Any]:
    if pid <= 0:
        return {"attempted": False, "reason": "pid_missing"}
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return {
                "attempted": True,
                "pid": pid,
                "success": completed.returncode == 0,
                "returncode": completed.returncode,
            }
        os.kill(pid, signal.SIGTERM)
        return {"attempted": True, "pid": pid, "success": True, "signal": "SIGTERM"}
    except ProcessLookupError:
        return {"attempted": True, "pid": pid, "success": True, "already_exited": True}
    except Exception as exc:
        return {"attempted": True, "pid": pid, "success": False, "error": str(exc)}


async def browser_close_session(params: Dict[str, Any]) -> Dict[str, Any]:
    """Close a managed browser session for one work_key and release CDP bookkeeping."""
    work_key = CDPSessionManager.normalize_work_key(params.get("work_key", "general"))
    close_tabs = _as_bool(params.get("close_tabs", True), default=True)
    close_browser = _as_bool(params.get("close_browser", True), default=True)
    keep_last = _as_bool(params.get("keep_last", False), default=False)
    allow_unmanaged_profile_close = _as_bool(params.get("allow_unmanaged_profile_close", False), default=False)
    reason = str(params.get("reason", "") or "")
    session = CDPSessionManager.get_session(work_key)
    if not session:
        return {
            "status": "success",
            "data": {
                "work_key": work_key,
                "closed_tabs": 0,
                "session_released": False,
                "process": {"attempted": False, "reason": "session_not_found"},
                "message": "CDP session already absent",
            },
        }

    port = session.port
    managed_profile = _is_managed_profile_dir(session.profile_dir)
    if not managed_profile and not allow_unmanaged_profile_close:
        CDPCommandGuardManager.force_release(CDPCommandGuardManager._guard_key({"work_key": work_key}, port=port))
        CDPSessionManager.release(work_key)
        logger.warning(
            "browser_close_session refused unmanaged profile work_key=%s port=%d profile=%s reason=%s",
            work_key,
            port,
            session.profile_dir,
            reason,
        )
        return {
            "status": "error",
            "data": {
                "error_code": "UNMANAGED_BROWSER_PROFILE",
                "message": "Refused to close a browser session outside the PC Agent managed profile root",
                "work_key": work_key,
                "port": port,
                "profile_dir": session.profile_dir,
                "session_released": True,
                "process": {"attempted": False, "reason": "unmanaged_profile"},
                "reason": reason,
            },
        }

    closed: list[dict[str, Any]] = []
    remaining = 0
    if close_tabs:
        try:
            targets = await _list_cdp_targets(port)
            pages = [
                t for t in targets
                if str(t.get("type") or "").lower() == "page" and str(t.get("webSocketDebuggerUrl") or "")
            ]
            remaining = len(pages)
            to_close = pages if not keep_last else pages[:-1]
            if to_close:
                browser_ws_url = await _get_browser_ws_url(port)
                for tab in to_close:
                    tid = str(tab.get("id") or tab.get("targetId") or "")
                    if not tid:
                        continue
                    try:
                        await _send_cdp(
                            browser_ws_url,
                            "Target.closeTarget",
                            {"targetId": tid},
                            timeout_seconds=5,
                        )
                        closed.append({
                            "id": tid,
                            "url": str(tab.get("url") or ""),
                            "title": str(tab.get("title") or ""),
                        })
                    except Exception as exc:
                        logger.warning("browser_close_session tab close failed work_key=%s target=%s err=%s", work_key, tid, exc)
                remaining = max(0, len(pages) - len(closed))
        except Exception as exc:
            logger.warning("browser_close_session list/close tabs failed work_key=%s err=%s", work_key, exc)

    process_result = {"attempted": False, "reason": "close_browser_false"}
    if close_browser:
        process_result = _terminate_browser_process(int(session.pid or 0))

    CDPCommandGuardManager.force_release(CDPCommandGuardManager._guard_key({"work_key": work_key}, port=port))
    CDPSessionManager.release(work_key)
    logger.info(
        "browser_close_session work_key=%s port=%d tabs=%d close_browser=%s reason=%s",
        work_key,
        port,
        len(closed),
        close_browser,
        reason,
    )
    return {
        "status": "success",
        "data": {
            "work_key": work_key,
            "port": port,
            "closed_tabs": len(closed),
            "closed_tab_details": closed,
            "remaining": remaining,
            "session_released": True,
            "process": process_result,
            "reason": reason,
        },
    }


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
    new_window = _as_bool(params.get("new_window", False), default=False)
    ready_timeout = float(params.get("ready_timeout_seconds", 15.0) or 15.0)
    window_args, window_metadata = _browser_window_cli_args(params)

    try:
        os.makedirs(profile_dir, exist_ok=True)

        existing_session = CDPSessionManager.get_session(work_key)
        if existing_session:
            existing = await _probe_cdp_version(existing_session.port)
            if existing is not None:
                if work_key.startswith("yeoljeong-bank-") and not await _bank_work_key_port_matches_url(
                    existing_session.port,
                    str(url or ""),
                ):
                    logger.warning(
                        "browser_launch_bank_work_key_port_mismatch work_key=%s port=%d url=%s",
                        work_key,
                        existing_session.port,
                        str(url or "")[:160],
                    )
                    CDPSessionManager.release(work_key)
                    existing = None
                else:
                    navigated = False
                    navigate_error = ""
                    if str(url or "").strip() and str(url).strip() != "about:blank":
                        try:
                            navigate_result = await browser_navigate(
                                {
                                    **params,
                                    "url": str(url),
                                    "port": existing_session.port,
                                    "work_key": work_key,
                                    "reuse_tab": False,
                                }
                            )
                            navigated = isinstance(navigate_result, dict) and navigate_result.get("status") == "success"
                            if not navigated:
                                navigate_data = navigate_result.get("data") if isinstance(navigate_result, dict) else {}
                                if isinstance(navigate_data, dict):
                                    navigate_error = str(
                                        navigate_data.get("error")
                                        or navigate_data.get("message")
                                        or navigate_data.get("error_code")
                                        or ""
                                    )[:200]
                        except Exception as exc:
                            navigate_error = str(exc)[:200]
                    return {
                        "status": "success",
                        "data": {
                            "message": f"기존 CDP 세션 사용 (port {existing_session.port})",
                            "port": existing_session.port,
                            "user_data_dir": existing_session.profile_dir,
                            "cdp_ready": True,
                            "websocket_debugger_url": existing.get("webSocketDebuggerUrl", ""),
                            "navigated": navigated,
                            "navigate_error": navigate_error,
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
                    if work_key.startswith("yeoljeong-bank-") and not await _bank_work_key_port_matches_url(
                        port,
                        str(url or ""),
                    ):
                        CDPSessionManager.release(work_key)
                        continue
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
                if (
                    work_key.startswith("yeoljeong-bank-")
                    and owner is None
                    and await _bank_work_key_port_matches_url(port, str(url or ""))
                ):
                    CDPSessionManager.register(work_key, port, profile_dir)
                    navigated = False
                    navigate_error = ""
                    if str(url or "").strip() and str(url).strip() != "about:blank":
                        try:
                            navigate_result = await browser_navigate(
                                {
                                    **params,
                                    "url": str(url),
                                    "port": port,
                                    "work_key": work_key,
                                    "reuse_tab": False,
                                }
                            )
                            navigated = isinstance(navigate_result, dict) and navigate_result.get("status") == "success"
                        except Exception as exc:
                            navigate_error = str(exc)[:200]
                    return {
                        "status": "success",
                        "data": {
                            "message": f"기존 은행 CDP 세션 재등록 (port {port})",
                            "port": port,
                            "user_data_dir": profile_dir,
                            "cdp_ready": True,
                            "websocket_debugger_url": existing.get("webSocketDebuggerUrl", ""),
                            "navigated": navigated,
                            "navigate_error": navigate_error,
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
            cmd.extend(window_args)
            if new_window:
                cmd.append("--new-window")
            cmd.append(url)

            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ready = await _wait_cdp_ready(port, ready_timeout)
            if ready is None:
                continue

            CDPSessionManager.register(work_key, port, profile_dir, pid=int(proc.pid or 0))
            logger.info("Chrome CDP 시작 완료 (port=%d profile=%s work_key=%s)", port, profile_dir, work_key)
            logger.info(
                "browser_launch_window_layout work_key=%s port=%d position=%s size=%s policy=%s",
                work_key,
                port,
                window_metadata.get("window_position", ""),
                window_metadata.get("window_size", ""),
                window_metadata.get("window_layout_policy", ""),
            )
            return {
                "status": "success",
                "data": {
                    "message": f"Chrome CDP 준비 완료 (port {port})",
                    "port": port,
                    "user_data_dir": profile_dir,
                    "cdp_ready": True,
                    "websocket_debugger_url": ready.get("webSocketDebuggerUrl", ""),
                    **window_metadata,
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
            cmd.extend(window_args)
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
                        **window_metadata,
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
