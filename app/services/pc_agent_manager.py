"""
AADS-195: PC 제어 에이전트 매니저 (싱글톤).
WebSocket 연결 관리, 명령 전송/결과 수신.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Optional, Set

from fastapi import WebSocket

from app.models.pc_agent import AgentInfo, CommandResult, StreamConfig, WSMessage

logger = logging.getLogger(__name__)

_DEVICE_COMMAND_WHITELIST = frozenset({
    "screenshot",
    "install_apk",
    "list_apps",
    "tap",
    "swipe",
    "input_text",
    "get_device_info",
})
_DEVICE_COMMAND_BLACKLIST = frozenset({
    "factory_reset",
    "root",
    "device_wipe",
    "wipe",
    "reboot_bootloader",
    "reboot_recovery",
})
_ANDROID_AGENT_COMMAND_MAP = {
    "screenshot": "screenshot",
    "list_apps": "app_list",
    "tap": "tap",
    "swipe": "swipe",
    "input_text": "key_input",
}
_ANDROID_DEVICE_INFO_PROPS = {
    "manufacturer": "ro.product.manufacturer",
    "brand": "ro.product.brand",
    "model": "ro.product.model",
    "device": "ro.product.device",
    "android_version": "ro.build.version.release",
    "sdk_int": "ro.build.version.sdk",
}

_ERROR_PC_AGENT_OFFLINE = "PC_AGENT_OFFLINE"
_ERROR_NO_CAPABLE_AGENT = "NO_CAPABLE_AGENT"
_ERROR_AGENT_BUSY = "AGENT_BUSY"
_ERROR_LEASE_EXPIRED = "LEASE_EXPIRED"
_ERROR_CDP_NOT_READY = "CDP_NOT_READY"
_ERROR_VVIC_LOGIN_REQUIRED = "VVIC_LOGIN_REQUIRED"
_ERROR_VVIC_BLOCKED = "VVIC_BLOCKED"
_ERROR_COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
_KNOWN_ROUTING_ERRORS = frozenset({
    _ERROR_PC_AGENT_OFFLINE,
    _ERROR_NO_CAPABLE_AGENT,
    _ERROR_AGENT_BUSY,
    _ERROR_LEASE_EXPIRED,
    _ERROR_CDP_NOT_READY,
    _ERROR_VVIC_LOGIN_REQUIRED,
    _ERROR_VVIC_BLOCKED,
    _ERROR_COMMAND_TIMEOUT,
})
_VVIC_JOB_TYPES = frozenset({"vvic", "vvic_cdp", "vvic_scrape"})
_DEFAULT_MAX_CONCURRENCY_BY_JOB = {
    "vvic_cdp": 1,
    "vvic": 1,
    "vvic_scrape": 1,
    "local_model_install": 1,
    "local_media_job": 1,
}


@dataclass
class _LeaseRecord:
    lease_id: str
    agent_id: str
    job_type: str
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    queue_position: int = 0
    command_type: str = ""
    required_capabilities: tuple[str, ...] = ()
    ttl_seconds: int = 180
    error_code: str = ""
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "agent_id": self.agent_id,
            "job_type": self.job_type,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "queue_position": self.queue_position,
            "command_type": self.command_type,
            "required_capabilities": list(self.required_capabilities),
            "ttl_seconds": self.ttl_seconds,
            "error_code": self.error_code or None,
            "error_message": self.error_message or None,
            "metadata": dict(self.metadata or {}),
        }


class _AgentConnection:
    """에이전트 WebSocket 연결 + 메타데이터."""

    def __init__(self, agent_id: str, websocket: WebSocket, info: AgentInfo) -> None:
        self.agent_id = agent_id
        self.websocket = websocket
        self.info = info


class PCAgentManager:
    """PC 에이전트 연결 및 명령 관리 (싱글톤)."""

    def __init__(self) -> None:
        self._agents: Dict[str, _AgentConnection] = {}
        self._pending_commands: Dict[str, asyncio.Event] = {}
        self._results: Dict[str, CommandResult] = {}
        self._streaming_subscribers: Dict[str, Set[WebSocket]] = {}  # agent_id → 대시보드 WS
        self._heartbeat_timeout_seconds = int(os.getenv("PC_AGENT_HEARTBEAT_TIMEOUT_SECONDS", "90") or "90")
        self._lease_default_ttl_seconds = int(os.getenv("PC_AGENT_LEASE_TTL_SECONDS", "180") or "180")
        self._lease_max_history = int(os.getenv("PC_AGENT_LEASE_HISTORY_MAX", "200") or "200")
        self._default_max_concurrency = int(os.getenv("PC_AGENT_DEFAULT_MAX_CONCURRENCY", "4") or "4")
        self._job_max_concurrency: Dict[str, int] = dict(_DEFAULT_MAX_CONCURRENCY_BY_JOB)
        self._lease_lock = asyncio.Lock()
        self._leases: Dict[str, _LeaseRecord] = {}
        self._lease_events: Dict[str, asyncio.Event] = {}
        self._lease_queues: Dict[tuple[str, str], Deque[str]] = {}
        self._running_leases: Dict[tuple[str, str], set[str]] = {}

    # ── 에이전트 등록/해제 ──────────────────────────────────────────

    def register_agent(
        self, agent_id: str, websocket: WebSocket, info: Dict[str, Any]
    ) -> AgentInfo:
        """에이전트 등록."""
        capabilities = self._normalize_capabilities(info.get("capabilities"))
        command_types = info.get("command_types")
        if isinstance(command_types, list):
            command_type_set = {str(item).strip().lower() for item in command_types if str(item).strip()}
            if "browser_launch" in command_type_set:
                capabilities.update({"chrome_cdp", "interactive_browser"})
        agent_info = AgentInfo(
            agent_id=agent_id,
            hostname=info.get("hostname", ""),
            os_info=info.get("os_info", ""),
            capabilities=sorted(capabilities),
        )
        self._agents[agent_id] = _AgentConnection(agent_id, websocket, agent_info)
        logger.info(
            "pc_agent_registered agent_id=%s hostname=%s capabilities=%s",
            agent_id,
            agent_info.hostname,
            ",".join(agent_info.capabilities),
        )
        return agent_info

    def unregister_agent(self, agent_id: str, websocket: WebSocket | None = None) -> bool:
        """에이전트 해제.

        websocket이 주어지면 현재 등록된 연결과 같을 때만 해제한다. 같은 agent_id의
        재연결이 새 WebSocket으로 교체된 뒤, 예전 연결의 finally가 새 연결을 지우는
        상황을 막기 위한 guard다.
        """
        conn = self._agents.get(agent_id)
        if conn is None:
            return False
        if websocket is not None and conn.websocket is not websocket:
            logger.info("pc_agent_unregister_skipped_stale agent_id=%s", agent_id)
            return False
        del self._agents[agent_id]
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._expire_agent_leases(agent_id, "agent websocket disconnected"))
        except RuntimeError:
            pass
        logger.info("pc_agent_unregistered agent_id=%s", agent_id)
        return True

    # ── 명령 전송/결과 ──────────────────────────────────────────────

    async def send_command(
        self, agent_id: str, command_type: str, params: Dict[str, Any]
    ) -> str:
        """에이전트에 명령 전송, command_id 반환."""
        conn = self._agents.get(agent_id)
        if conn is None:
            raise ValueError(f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")

        command_id = str(uuid.uuid4())

        # 결과 대기용 이벤트 생성
        self._pending_commands[command_id] = asyncio.Event()
        self._results[command_id] = CommandResult(
            command_id=command_id,
            agent_id=agent_id,
        )

        # WebSocket으로 명령 전송
        msg = WSMessage(
            type="command",
            id=command_id,
            payload={"command_type": command_type, "params": params},
        )
        await conn.websocket.send_json(msg.model_dump(mode="json"))
        logger.info(
            "pc_agent_command_sent agent_id=%s command_id=%s type=%s",
            agent_id, command_id, command_type,
        )
        return command_id

    async def get_result(self, command_id: str, timeout: float = 30.0) -> CommandResult:
        """명령 결과 대기."""
        event = self._pending_commands.get(command_id)
        if event is None:
            result = self._results.get(command_id)
            if result:
                return result
            raise ValueError(f"command_id '{command_id}'를 찾을 수 없습니다.")

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            result = self._results.get(command_id)
            if result:
                result.status = "timeout"
                result.completed_at = datetime.utcnow()
            self._pending_commands.pop(command_id, None)
            logger.warning("pc_agent_command_timeout command_id=%s", command_id)
            if result:
                return result
            return CommandResult(
                command_id=command_id, agent_id="", status="timeout",
            )

        self._pending_commands.pop(command_id, None)
        return self._results[command_id]

    def receive_result(self, command_id: str, result: Dict[str, Any]) -> None:
        """에이전트로부터 결과 수신."""
        stored = self._results.get(command_id)
        if stored is None:
            logger.warning("pc_agent_unknown_result command_id=%s", command_id)
            return

        stored.status = result.get("status", "success")
        stored.result = result.get("data")
        stored.completed_at = datetime.utcnow()

        event = self._pending_commands.get(command_id)
        if event:
            event.set()
        logger.info("pc_agent_result_received command_id=%s status=%s", command_id, stored.status)

    def update_heartbeat(self, agent_id: str) -> None:
        """에이전트 하트비트 갱신."""
        conn = self._agents.get(agent_id)
        if conn:
            conn.info.last_heartbeat = datetime.utcnow()

    # ── 스트리밍 ──────────────────────────────────────────────────

    def add_stream_subscriber(self, agent_id: str, ws: WebSocket) -> None:
        """스트리밍 구독자 등록."""
        if agent_id not in self._streaming_subscribers:
            self._streaming_subscribers[agent_id] = set()
        self._streaming_subscribers[agent_id].add(ws)
        logger.info("stream_subscriber_added agent_id=%s total=%d", agent_id, len(self._streaming_subscribers[agent_id]))

    def remove_stream_subscriber(self, agent_id: str, ws: WebSocket) -> int:
        """스트리밍 구독자 해제. 남은 구독자 수 반환."""
        subs = self._streaming_subscribers.get(agent_id)
        if subs:
            subs.discard(ws)
            remaining = len(subs)
            if remaining == 0:
                del self._streaming_subscribers[agent_id]
            logger.info("stream_subscriber_removed agent_id=%s remaining=%d", agent_id, remaining)
            return remaining
        return 0

    async def start_stream(self, agent_id: str, config: StreamConfig) -> str:
        """에이전트에 스트리밍 시작 명령 전송."""
        conn = self._agents.get(agent_id)
        if conn is None:
            raise ValueError(f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")

        command_id = str(uuid.uuid4())
        msg = WSMessage(
            type="command",
            id=command_id,
            payload={"command_type": "stream_start", "params": config.model_dump()},
        )
        await conn.websocket.send_json(msg.model_dump(mode="json"))
        logger.info("stream_start_sent agent_id=%s config=%s", agent_id, config.model_dump())
        return command_id

    async def stop_stream(self, agent_id: str) -> str:
        """에이전트에 스트리밍 중지 명령 전송."""
        conn = self._agents.get(agent_id)
        if conn is None:
            raise ValueError(f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")

        command_id = str(uuid.uuid4())
        msg = WSMessage(
            type="command",
            id=command_id,
            payload={"command_type": "stream_stop", "params": {}},
        )
        await conn.websocket.send_json(msg.model_dump(mode="json"))
        logger.info("stream_stop_sent agent_id=%s", agent_id)
        return command_id

    async def broadcast_frame(self, agent_id: str, frame_data: str) -> None:
        """모든 구독자에게 스트리밍 프레임 전송."""
        subs = self._streaming_subscribers.get(agent_id)
        if not subs:
            return

        msg = {"type": "stream_frame", "frame": frame_data}
        dead: list[WebSocket] = []
        for ws in subs:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)

        for ws in dead:
            subs.discard(ws)
        if dead:
            logger.debug("stream_dead_subscribers agent_id=%s removed=%d", agent_id, len(dead))
            if not subs:
                del self._streaming_subscribers[agent_id]

    # ── 종료 ──────────────────────────────────────────────────────

    async def close_all_connections(self, reason: str = "server_shutdown") -> int:
        """모든 에이전트 연결을 1012로 정상 종료."""
        closed = 0
        for agent_id, conn in list(self._agents.items()):
            try:
                await conn.websocket.close(code=1012, reason=reason)
                closed += 1
            except Exception:
                pass
        self._agents.clear()
        self._streaming_subscribers.clear()
        logger.info("pc_agent_all_connections_closed count=%d reason=%s", closed, reason)
        return closed

    # ── 조회 ──────────────────────────────────────────────────────

    def list_agents(self) -> list[AgentInfo]:
        """연결된 에이전트 목록."""
        return [conn.info for conn in self._agents.values()]

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """특정 에이전트 정보 조회."""
        conn = self._agents.get(agent_id)
        return conn.info if conn else None

    def connected_count(self) -> int:
        return len(self._agents)

    # ── Routing / Lease / Queue ───────────────────────────────────────

    def _now(self) -> datetime:
        return datetime.utcnow()

    def _normalize_capabilities(self, value: Any) -> set[str]:
        if not isinstance(value, (list, tuple, set)):
            return set()
        normalized: set[str] = set()
        for raw in value:
            cap = str(raw or "").strip().lower().replace("-", "_")
            if cap:
                normalized.add(cap)
        return normalized

    def _normalize_job_type(self, value: str) -> str:
        job_type = str(value or "general").strip().lower().replace("-", "_")
        return job_type or "general"

    def _normalize_error_code(self, value: str) -> str:
        code = str(value or "").strip().upper()
        return code if code in _KNOWN_ROUTING_ERRORS else ""

    def _is_online_locked(self, agent_id: str, now: datetime | None = None) -> bool:
        conn = self._agents.get(agent_id)
        if conn is None:
            return False
        ts = now or self._now()
        delta = (ts - conn.info.last_heartbeat).total_seconds()
        return delta <= self._heartbeat_timeout_seconds

    def _max_concurrency_for_job(self, job_type: str) -> int:
        return max(1, int(self._job_max_concurrency.get(job_type, self._default_max_concurrency)))

    def _lease_key(self, agent_id: str, job_type: str) -> tuple[str, str]:
        return (agent_id, job_type)

    def _queue_for_key_locked(self, key: tuple[str, str]) -> Deque[str]:
        queue = self._lease_queues.get(key)
        if queue is None:
            queue = deque()
            self._lease_queues[key] = queue
        return queue

    def _running_for_key_locked(self, key: tuple[str, str]) -> set[str]:
        running = self._running_leases.get(key)
        if running is None:
            running = set()
            self._running_leases[key] = running
        return running

    def _remove_from_queue_locked(self, key: tuple[str, str], lease_id: str) -> None:
        queue = self._lease_queues.get(key)
        if not queue:
            return
        filtered = deque(item for item in queue if item != lease_id)
        if filtered:
            self._lease_queues[key] = filtered
        else:
            self._lease_queues.pop(key, None)

    def _refresh_queue_positions_locked(self, key: tuple[str, str]) -> None:
        queue = self._lease_queues.get(key)
        if not queue:
            return
        for idx, lease_id in enumerate(queue):
            lease = self._leases.get(lease_id)
            if lease and lease.status == "queued":
                lease.queue_position = idx + 1

    def _finalize_lease_locked(
        self,
        lease: _LeaseRecord,
        *,
        status: str,
        now: datetime,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        key = self._lease_key(lease.agent_id, lease.job_type)
        self._running_for_key_locked(key).discard(lease.lease_id)
        self._remove_from_queue_locked(key, lease.lease_id)
        if not self._running_leases.get(key):
            self._running_leases.pop(key, None)
        if not self._lease_queues.get(key):
            self._lease_queues.pop(key, None)
        lease.status = status
        lease.updated_at = now
        lease.expires_at = now
        lease.error_code = self._normalize_error_code(error_code)
        lease.error_message = error_message or lease.error_message
        lease.queue_position = 0
        event = self._lease_events.get(lease.lease_id)
        if event:
            event.set()

    def _promote_next_locked(self, key: tuple[str, str], now: datetime) -> None:
        queue = self._lease_queues.get(key)
        if not queue:
            return
        running = self._running_for_key_locked(key)
        max_concurrency = self._max_concurrency_for_job(key[1])
        while queue and len(running) < max_concurrency:
            lease_id = queue.popleft()
            lease = self._leases.get(lease_id)
            if lease is None:
                continue
            if lease.status != "queued":
                continue
            if lease.expires_at <= now:
                self._finalize_lease_locked(
                    lease,
                    status="expired",
                    now=now,
                    error_code=_ERROR_LEASE_EXPIRED,
                    error_message="lease expired before execution",
                )
                continue
            lease.status = "running"
            lease.queue_position = 0
            lease.updated_at = now
            lease.expires_at = now + timedelta(seconds=lease.ttl_seconds)
            running.add(lease.lease_id)
            event = self._lease_events.get(lease.lease_id)
            if event:
                event.set()
        if queue:
            self._lease_queues[key] = queue
            self._refresh_queue_positions_locked(key)
        else:
            self._lease_queues.pop(key, None)

    def _cleanup_stale_leases_locked(self, now: datetime) -> None:
        affected_keys: set[tuple[str, str]] = set()
        for lease in list(self._leases.values()):
            if lease.status not in {"running", "queued"}:
                continue
            if lease.expires_at > now:
                continue
            self._finalize_lease_locked(
                lease,
                status="expired",
                now=now,
                error_code=_ERROR_LEASE_EXPIRED,
                error_message="lease stale timeout",
            )
            affected_keys.add(self._lease_key(lease.agent_id, lease.job_type))

        for key in affected_keys:
            self._promote_next_locked(key, now)

        finalized = [
            lease
            for lease in self._leases.values()
            if lease.status in {"completed", "error", "expired", "cancelled"}
        ]
        if len(finalized) <= self._lease_max_history:
            return
        finalized.sort(key=lambda item: item.updated_at)
        for lease in finalized[: len(finalized) - self._lease_max_history]:
            self._leases.pop(lease.lease_id, None)
            self._lease_events.pop(lease.lease_id, None)

    def _select_agent_locked(
        self,
        *,
        preferred_agent_id: str,
        required_capabilities: set[str],
        job_type: str,
        now: datetime,
    ) -> dict[str, Any]:
        preferred = str(preferred_agent_id or "").strip()

        if preferred:
            conn = self._agents.get(preferred)
            if conn is None or not self._is_online_locked(preferred, now):
                return {
                    "error_code": _ERROR_PC_AGENT_OFFLINE,
                    "message": f"agent '{preferred}' is offline",
                }
            capabilities = {cap.lower() for cap in conn.info.capabilities}
            missing = sorted(required_capabilities - capabilities)
            if missing:
                return {
                    "error_code": _ERROR_NO_CAPABLE_AGENT,
                    "message": f"agent '{preferred}' missing capabilities: {', '.join(missing)}",
                    "missing_capabilities": missing,
                }
            return {"agent_id": preferred}

        online_agents = [
            conn
            for conn in self._agents.values()
            if self._is_online_locked(conn.agent_id, now)
        ]
        if not online_agents:
            return {
                "error_code": _ERROR_PC_AGENT_OFFLINE,
                "message": "no online PC agent",
            }

        capable: list[_AgentConnection] = []
        for conn in online_agents:
            caps = {cap.lower() for cap in conn.info.capabilities}
            if required_capabilities.issubset(caps):
                capable.append(conn)
        if not capable:
            return {
                "error_code": _ERROR_NO_CAPABLE_AGENT,
                "message": "no capable PC agent for requested capabilities",
                "required_capabilities": sorted(required_capabilities),
            }

        def _score(conn: _AgentConnection) -> tuple[int, int, float]:
            key = self._lease_key(conn.agent_id, job_type)
            running = len(self._running_leases.get(key, set()))
            queued = len(self._lease_queues.get(key, deque()))
            heartbeat_age = (now - conn.info.last_heartbeat).total_seconds()
            return (running, queued, heartbeat_age)

        chosen = min(capable, key=_score)
        return {"agent_id": chosen.agent_id}

    def _map_error_code_from_result(self, result: CommandResult) -> str:
        payload = result.result if isinstance(result.result, dict) else {}
        explicit = self._normalize_error_code(str(payload.get("error_code", "")))
        if explicit:
            return explicit

        message = str(payload.get("error", "") or payload.get("message", "")).lower()
        if "cdp_not_ready" in message or ("cdp" in message and "ready" in message):
            return _ERROR_CDP_NOT_READY
        if "login required" in message or "vvic_login_required" in message or "로그인" in message:
            return _ERROR_VVIC_LOGIN_REQUIRED
        if "blocked" in message or "captcha" in message or "vvic_blocked" in message or "차단" in message:
            return _ERROR_VVIC_BLOCKED
        return ""

    def _prepare_vvic_browser_launch_params(self, params: Dict[str, Any], lease_id: str) -> Dict[str, Any]:
        merged = dict(params or {})
        merged.setdefault("isolated_profile", True)
        merged.setdefault("isolation_id", lease_id)
        merged.setdefault("dynamic_port", True)
        merged.setdefault("preferred_port", 9222)
        merged.setdefault("port_candidates", [9222, 9333, 9444, 9555, 9666, 9777])
        merged.setdefault("require_cdp_ready", True)
        merged.setdefault("new_window", True)
        return merged

    async def acquire_lease(
        self,
        *,
        job_type: str,
        command_type: str,
        preferred_agent_id: str = "",
        required_capabilities: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        normalized_job = self._normalize_job_type(job_type)
        required = self._normalize_capabilities(required_capabilities or [])
        if normalized_job in _VVIC_JOB_TYPES:
            required.update({"vvic", "chrome_cdp", "interactive_browser"})
            normalized_job = "vvic_cdp"

        async with self._lease_lock:
            now = self._now()
            self._cleanup_stale_leases_locked(now)

            selected = self._select_agent_locked(
                preferred_agent_id=preferred_agent_id,
                required_capabilities=required,
                job_type=normalized_job,
                now=now,
            )
            selected_agent_id = str(selected.get("agent_id", "") or "")
            if not selected_agent_id:
                return {
                    "status": "error",
                    "error_code": selected.get("error_code", _ERROR_NO_CAPABLE_AGENT),
                    "message": selected.get("message", "agent selection failed"),
                    "required_capabilities": sorted(required),
                }

            key = self._lease_key(selected_agent_id, normalized_job)
            running = self._running_for_key_locked(key)
            max_concurrency = self._max_concurrency_for_job(normalized_job)
            ttl = max(int(ttl_seconds or self._lease_default_ttl_seconds), 30)
            lease_id = str(uuid.uuid4())
            status = "running" if len(running) < max_concurrency else "queued"

            lease = _LeaseRecord(
                lease_id=lease_id,
                agent_id=selected_agent_id,
                job_type=normalized_job,
                status=status,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=ttl),
                command_type=command_type,
                required_capabilities=tuple(sorted(required)),
                ttl_seconds=ttl,
            )
            self._leases[lease_id] = lease
            self._lease_events[lease_id] = asyncio.Event()

            if status == "running":
                running.add(lease_id)
            else:
                queue = self._queue_for_key_locked(key)
                queue.append(lease_id)
                lease.queue_position = len(queue)

            logger.info(
                "pc_agent_lease_acquired lease_id=%s agent_id=%s job_type=%s status=%s queue=%d",
                lease_id,
                selected_agent_id,
                normalized_job,
                status,
                lease.queue_position,
            )
            payload = lease.public_dict()
            payload["max_concurrency"] = max_concurrency
            return {"status": status, "lease": payload}

    async def wait_for_lease_turn(self, lease_id: str, timeout_seconds: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.1)
        while True:
            event: asyncio.Event | None = None
            async with self._lease_lock:
                now = self._now()
                self._cleanup_stale_leases_locked(now)
                lease = self._leases.get(lease_id)
                if lease is None:
                    return {
                        "status": "error",
                        "error_code": _ERROR_LEASE_EXPIRED,
                        "message": "lease not found",
                    }
                if lease.status == "running":
                    return {"status": "running", "lease": lease.public_dict()}
                if lease.status in {"expired", "cancelled", "error", "completed"}:
                    return {
                        "status": "error",
                        "error_code": lease.error_code or _ERROR_LEASE_EXPIRED,
                        "message": lease.error_message or f"lease status={lease.status}",
                        "lease": lease.public_dict(),
                    }
                event = self._lease_events.get(lease_id)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._finalize_lease_locked(
                        lease,
                        status="expired",
                        now=now,
                        error_code=_ERROR_AGENT_BUSY,
                        error_message="queue wait timeout",
                    )
                    self._promote_next_locked(self._lease_key(lease.agent_id, lease.job_type), now)
                    return {
                        "status": "error",
                        "error_code": _ERROR_AGENT_BUSY,
                        "message": "queue wait timeout",
                        "lease": lease.public_dict(),
                    }

            if event is None:
                await asyncio.sleep(0.1)
                continue
            remaining = max(0.1, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(event.wait(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                continue
            event.clear()

    async def heartbeat_lease(self, lease_id: str, *, extend_seconds: int | None = None) -> dict[str, Any]:
        async with self._lease_lock:
            now = self._now()
            self._cleanup_stale_leases_locked(now)
            lease = self._leases.get(lease_id)
            if lease is None:
                return {
                    "status": "error",
                    "error_code": _ERROR_LEASE_EXPIRED,
                    "message": "lease not found",
                }
            if lease.status not in {"running", "queued"}:
                return {
                    "status": "error",
                    "error_code": lease.error_code or _ERROR_LEASE_EXPIRED,
                    "message": lease.error_message or f"lease status={lease.status}",
                    "lease": lease.public_dict(),
                }
            ttl = max(int(extend_seconds or lease.ttl_seconds), 30)
            lease.ttl_seconds = ttl
            lease.updated_at = now
            lease.expires_at = now + timedelta(seconds=ttl)
            return {"status": "ok", "lease": lease.public_dict()}

    async def release_lease(
        self,
        lease_id: str,
        *,
        status: str = "completed",
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        async with self._lease_lock:
            now = self._now()
            self._cleanup_stale_leases_locked(now)
            lease = self._leases.get(lease_id)
            if lease is None:
                return {
                    "status": "error",
                    "error_code": _ERROR_LEASE_EXPIRED,
                    "message": "lease not found",
                }
            if lease.status in {"completed", "expired", "cancelled", "error"}:
                return {"status": lease.status, "lease": lease.public_dict()}
            final_status = status if status in {"completed", "expired", "cancelled", "error"} else "completed"
            self._finalize_lease_locked(
                lease,
                status=final_status,
                now=now,
                error_code=error_code,
                error_message=error_message,
            )
            self._promote_next_locked(self._lease_key(lease.agent_id, lease.job_type), now)
            logger.info(
                "pc_agent_lease_released lease_id=%s status=%s error_code=%s",
                lease_id,
                final_status,
                error_code,
            )
            return {"status": final_status, "lease": lease.public_dict()}

    async def list_leases(
        self,
        *,
        agent_id: str = "",
        job_type: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        normalized_job = self._normalize_job_type(job_type) if job_type else ""
        status_filter = str(status or "").strip().lower()
        async with self._lease_lock:
            now = self._now()
            self._cleanup_stale_leases_locked(now)
            items: list[_LeaseRecord] = list(self._leases.values())
            if agent_id:
                items = [item for item in items if item.agent_id == agent_id]
            if normalized_job:
                items = [item for item in items if item.job_type == normalized_job]
            if status_filter:
                items = [item for item in items if item.status == status_filter]
            items.sort(key=lambda item: item.created_at, reverse=True)
            return [item.public_dict() for item in items]

    async def get_lease(self, lease_id: str) -> dict[str, Any] | None:
        async with self._lease_lock:
            now = self._now()
            self._cleanup_stale_leases_locked(now)
            lease = self._leases.get(lease_id)
            return lease.public_dict() if lease else None

    async def _expire_agent_leases(self, agent_id: str, reason: str) -> None:
        async with self._lease_lock:
            now = self._now()
            affected: set[tuple[str, str]] = set()
            for lease in self._leases.values():
                if lease.agent_id != agent_id:
                    continue
                if lease.status not in {"running", "queued"}:
                    continue
                self._finalize_lease_locked(
                    lease,
                    status="expired",
                    now=now,
                    error_code=_ERROR_PC_AGENT_OFFLINE,
                    error_message=reason,
                )
                affected.add(self._lease_key(lease.agent_id, lease.job_type))
            for key in affected:
                self._promote_next_locked(key, now)

    async def execute_routed_command(
        self,
        *,
        command_type: str,
        params: Dict[str, Any] | None = None,
        agent_id: str = "",
        job_type: str = "general",
        required_capabilities: list[str] | None = None,
        queue_if_busy: bool = True,
        wait_for_turn: bool = True,
        queue_wait_timeout_seconds: float = 120.0,
        lease_ttl_seconds: int = 180,
        command_timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        request_params: Dict[str, Any] = dict(params or {})
        lease_response = await self.acquire_lease(
            job_type=job_type,
            command_type=command_type,
            preferred_agent_id=agent_id,
            required_capabilities=required_capabilities,
            ttl_seconds=lease_ttl_seconds,
        )
        if lease_response.get("status") == "error":
            return lease_response

        lease_payload = lease_response.get("lease", {})
        lease_id = str(lease_payload.get("lease_id", "") or "")
        lease_status = str(lease_response.get("status", "queued"))

        if lease_status == "queued" and not queue_if_busy:
            await self.release_lease(
                lease_id,
                status="cancelled",
                error_code=_ERROR_AGENT_BUSY,
                error_message="agent busy",
            )
            updated = await self.get_lease(lease_id)
            return {
                "status": "error",
                "error_code": _ERROR_AGENT_BUSY,
                "message": "agent busy",
                "lease": updated or lease_payload,
            }

        if lease_status == "queued" and wait_for_turn:
            wait_result = await self.wait_for_lease_turn(lease_id, timeout_seconds=queue_wait_timeout_seconds)
            if wait_result.get("status") == "error":
                return wait_result
            lease_payload = wait_result.get("lease", lease_payload)
        elif lease_status == "queued":
            return {
                "status": "queued",
                "lease": lease_payload,
                "message": "queued behind running lease",
            }

        final_job_type = str(lease_payload.get("job_type", "") or "")
        if final_job_type in _VVIC_JOB_TYPES and command_type == "browser_launch":
            request_params = self._prepare_vvic_browser_launch_params(request_params, lease_id)

        await self.heartbeat_lease(lease_id, extend_seconds=max(lease_ttl_seconds, int(command_timeout_seconds) + 30))
        selected_agent_id = str(lease_payload.get("agent_id", "") or "")
        try:
            command_id = await self.send_command(selected_agent_id, command_type, request_params)
        except ValueError:
            await self.release_lease(
                lease_id,
                status="expired",
                error_code=_ERROR_PC_AGENT_OFFLINE,
                error_message="agent disconnected before command dispatch",
            )
            refreshed = await self.get_lease(lease_id)
            return {
                "status": "error",
                "error_code": _ERROR_PC_AGENT_OFFLINE,
                "message": "agent disconnected before command dispatch",
                "lease": refreshed or lease_payload,
            }

        command_result = await self.get_result(command_id, timeout=command_timeout_seconds)
        lease_for_return = await self.get_lease(lease_id)

        if command_result.status == "timeout":
            await self.release_lease(
                lease_id,
                status="error",
                error_code=_ERROR_COMMAND_TIMEOUT,
                error_message="command timeout",
            )
            refreshed = await self.get_lease(lease_id)
            return {
                "status": "error",
                "error_code": _ERROR_COMMAND_TIMEOUT,
                "message": "command timeout",
                "command_id": command_id,
                "lease": refreshed or lease_for_return or lease_payload,
                "result": command_result.model_dump(mode="json"),
            }

        if command_result.status == "error":
            mapped_error = self._map_error_code_from_result(command_result)
            await self.release_lease(
                lease_id,
                status="error",
                error_code=mapped_error,
                error_message=str((command_result.result or {}).get("error", "command failed")),
            )
            refreshed = await self.get_lease(lease_id)
            return {
                "status": "error",
                "error_code": mapped_error or None,
                "message": str((command_result.result or {}).get("error", "command failed")),
                "command_id": command_id,
                "lease": refreshed or lease_for_return or lease_payload,
                "result": command_result.model_dump(mode="json"),
            }

        await self.release_lease(lease_id, status="completed")
        refreshed = await self.get_lease(lease_id)
        return {
            "status": "success",
            "command_id": command_id,
            "lease": refreshed or lease_for_return or lease_payload,
            "result": command_result.model_dump(mode="json"),
        }

    # ── Android device_command ───────────────────────────────────────

    async def execute_device_command(
        self,
        device_id: str,
        command: str,
        args: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """MCP용 Android 통합 device_command 실행."""
        command = (command or "").strip().lower()
        args = args if isinstance(args, dict) else {}

        if not command:
            return {
                "status": "error",
                "error": "command 필수",
                "allowed_commands": sorted(_DEVICE_COMMAND_WHITELIST),
            }
        nested_commands = {
            str(args.get("command") or "").strip().lower(),
            str(args.get("action") or "").strip().lower(),
            str(args.get("subcommand") or "").strip().lower(),
        }
        if command in _DEVICE_COMMAND_BLACKLIST:
            return {
                "status": "error",
                "error": f"위험 명령 '{command}'은(는) 차단되었습니다.",
            }
        if any(value in _DEVICE_COMMAND_BLACKLIST for value in nested_commands if value):
            return {
                "status": "error",
                "error": "위험 명령 파라미터가 감지되어 요청이 차단되었습니다.",
            }
        if command not in _DEVICE_COMMAND_WHITELIST:
            return {
                "status": "error",
                "error": f"지원하지 않는 command: {command}",
                "allowed_commands": sorted(_DEVICE_COMMAND_WHITELIST),
            }

        if command == "install_apk":
            return await self._execute_adb_device_command(device_id, command, args)

        android_agents = self._list_android_agents()
        if not device_id and len(android_agents) > 1:
            return {
                "status": "error",
                "command": command,
                "backend": "android_agent",
                "error": "여러 Android Agent가 연결되어 있습니다. device_id를 지정해주세요.",
                "available_devices": android_agents,
            }

        agent_id = self._resolve_android_agent_id(device_id)
        if agent_id:
            agent_result = await self._execute_android_agent_command(agent_id, command, args)
            if agent_result.get("status") == "success":
                return agent_result
            if device_id or not agent_result.get("fallback_to_adb"):
                agent_result.pop("fallback_to_adb", None)
                return agent_result

        return await self._execute_adb_device_command(device_id, command, args)

    def _resolve_android_agent_id(self, device_id: str) -> str:
        android_devices = self._list_android_agents()
        if not android_devices:
            return ""

        if device_id:
            match = next(
                (
                    str(device.get("device_id") or "")
                    for device in android_devices
                    if device.get("device_id") == device_id
                ),
                "",
            )
            return match

        if len(android_devices) == 1:
            return str(android_devices[0].get("device_id") or "")

        return ""

    def _list_android_agents(self) -> list[Dict[str, Any]]:
        from app.services.device_manager import device_manager

        android_devices = device_manager.get_devices("android")
        result: list[Dict[str, Any]] = []
        for device in android_devices:
            result.append(
                {
                    "agent_id": device.get("agent_id"),
                    "device_id": device.get("agent_id"),
                    "backend": "android_agent",
                    "status": "connected",
                    "hostname": device.get("hostname", ""),
                    "capabilities": device.get("capabilities", []),
                }
            )
        return result

    async def _execute_android_agent_command(
        self,
        agent_id: str,
        command: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        from app.services.device_manager import device_manager

        capabilities = set(device_manager.get_device_capabilities(agent_id))

        if command == "get_device_info":
            required_caps = {"permission_status", "shell_limited"}
            if not required_caps.issubset(capabilities):
                return {
                    "status": "error",
                    "device_id": agent_id,
                    "command": command,
                    "backend": "android_agent",
                    "error": "연결된 Android Agent가 get_device_info에 필요한 capability를 지원하지 않습니다.",
                    "fallback_to_adb": True,
                }
            return await self._get_android_agent_device_info(agent_id)

        mapped_command = _ANDROID_AGENT_COMMAND_MAP.get(command)
        if not mapped_command or mapped_command not in capabilities:
            return {
                "status": "error",
                "device_id": agent_id,
                "command": command,
                "backend": "android_agent",
                "error": f"연결된 Android Agent가 '{command}' 명령을 지원하지 않습니다.",
                "fallback_to_adb": True,
            }

        normalized_args = self._normalize_android_agent_args(command, args)
        result = await device_manager.send_command(
            agent_id,
            mapped_command,
            normalized_args,
            timeout=self._coerce_float(args.get("timeout"), default=30.0),
        )
        data = result.data or {}
        if result.status != "success":
            return {
                "status": "error",
                "device_id": agent_id,
                "command": command,
                "backend": "android_agent",
                "error": str(data.get("error") or f"Android Agent 명령 실패: {command}"),
                "data": data,
                "fallback_to_adb": False,
            }

        if command == "screenshot":
            data = self._normalize_screenshot_payload(data, args)

        return {
            "status": "success",
            "device_id": agent_id,
            "command": command,
            "backend": "android_agent",
            "data": data,
        }

    async def _get_android_agent_device_info(self, agent_id: str) -> Dict[str, Any]:
        from app.services.device_manager import device_manager

        commands: list[tuple[str, str, Dict[str, Any], float]] = [
            ("permission_status", "permission_status", {}, 15.0),
        ]
        for field, prop in _ANDROID_DEVICE_INFO_PROPS.items():
            commands.append(
                (field, "shell_limited", {"command": f"getprop {prop}"}, 10.0)
            )
        commands.append(
            ("android_id", "shell_limited", {"command": "settings get secure android_id"}, 10.0)
        )
        commands.append(
            ("kernel", "shell_limited", {"command": "uname -a"}, 10.0)
        )

        results = await asyncio.gather(
            *[
                device_manager.send_command(agent_id, command_type, params, timeout=timeout)
                for _, command_type, params, timeout in commands
            ],
            return_exceptions=True,
        )

        device_info = device_manager.get_device(agent_id)
        data: Dict[str, Any] = {
            "agent_id": agent_id,
            "device_type": "android",
            "capabilities": device_manager.get_device_capabilities(agent_id),
        }
        if device_info is not None:
            data["connected_at"] = device_info.connected_at.isoformat()
            if device_info.hostname:
                data["hostname"] = device_info.hostname
            if device_info.os_info:
                data["os_info"] = device_info.os_info

        for (field, _, _, _), result in zip(commands, results):
            if isinstance(result, Exception):
                logger.warning("android_agent_device_info_failed agent_id=%s field=%s err=%s", agent_id, field, result)
                continue
            payload = result.data or {}
            if result.status != "success":
                continue
            if field == "permission_status":
                data["permission_status"] = payload
                package_name = str(payload.get("package") or "").strip()
                if package_name:
                    data["package"] = package_name
                sdk_value = payload.get("sdk_int")
                if sdk_value not in (None, ""):
                    data["sdk_int"] = self._coerce_int(sdk_value)
                continue
            value = str(payload.get("stdout") or "").strip()
            if value:
                data[field] = value

        sdk_value = data.get("sdk_int")
        if isinstance(sdk_value, str):
            data["sdk_int"] = self._coerce_int(sdk_value)

        return {
            "status": "success",
            "device_id": agent_id,
            "command": "get_device_info",
            "backend": "android_agent",
            "data": data,
        }

    def _normalize_android_agent_args(
        self,
        command: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        if command == "input_text":
            return {
                "text": str(args.get("text") or ""),
                "append": bool(args.get("append", True)),
            }
        if command == "swipe":
            return {
                "x1": self._coerce_int(args.get("x1")),
                "y1": self._coerce_int(args.get("y1")),
                "x2": self._coerce_int(args.get("x2")),
                "y2": self._coerce_int(args.get("y2")),
                "duration_ms": self._coerce_int(
                    args.get("duration_ms", args.get("duration", 400)),
                    default=400,
                ),
            }
        if command == "tap":
            return {
                "x": self._coerce_int(args.get("x")),
                "y": self._coerce_int(args.get("y")),
            }
        if command == "screenshot":
            return {
                "timeout_ms": self._coerce_int(args.get("timeout_ms"), default=8000),
            }
        return dict(args)

    async def _execute_adb_device_command(
        self,
        device_id: str,
        command: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        resolved_device_id, error_result = await asyncio.to_thread(
            self._resolve_adb_device_id,
            device_id,
        )
        if error_result is not None:
            return error_result

        try:
            result = await asyncio.to_thread(
                self._execute_adb_device_command_sync,
                resolved_device_id,
                command,
                args,
            )
        except ValueError as exc:
            return {
                "status": "error",
                "device_id": resolved_device_id,
                "command": command,
                "backend": "adb",
                "error": str(exc),
            }
        except Exception as exc:
            logger.exception(
                "device_command_adb_failed device_id=%s command=%s err=%s",
                resolved_device_id,
                command,
                exc,
            )
            return {
                "status": "error",
                "device_id": resolved_device_id,
                "command": command,
                "backend": "adb",
                "error": str(exc),
            }

        result["device_id"] = resolved_device_id
        result["command"] = command
        result["backend"] = "adb"
        return result

    def _resolve_adb_device_id(
        self,
        device_id: str,
    ) -> tuple[str, Dict[str, Any] | None]:
        try:
            devices = self._list_adb_devices()
        except RuntimeError as exc:
            return "", {
                "status": "error",
                "command": "device_command",
                "backend": "adb",
                "error": str(exc),
            }

        connected = [device for device in devices if device.get("status") == "device"]

        if device_id:
            match = next(
                (device for device in devices if device.get("device_id") == device_id),
                None,
            )
            if match is None:
                return "", {
                    "status": "error",
                    "command": "device_command",
                    "backend": "adb",
                    "error": f"Android 디바이스 '{device_id}'가 연결되어 있지 않습니다.",
                    "available_devices": connected,
                }
            if match.get("status") != "device":
                return "", {
                    "status": "error",
                    "command": "device_command",
                    "backend": "adb",
                    "error": (
                        f"Android 디바이스 '{device_id}' 상태가 '{match.get('status')}'입니다. "
                        "USB 디버깅 승인 상태를 확인하세요."
                    ),
                    "available_devices": connected,
                }
            return str(match["device_id"]), None

        if len(connected) == 1:
            return str(connected[0]["device_id"]), None

        if not connected:
            return "", {
                "status": "error",
                "command": "device_command",
                "backend": "adb",
                "error": "연결된 Android 디바이스가 없습니다. USB/ADB 연결 상태를 확인하세요.",
                "available_devices": [],
            }

        return "", {
            "status": "error",
            "command": "device_command",
            "backend": "adb",
            "error": "여러 Android 디바이스가 연결되어 있습니다. device_id를 지정해주세요.",
            "available_devices": connected,
        }

    def _list_adb_devices(self) -> list[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["adb", "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("ADB가 설치되어 있지 않습니다.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ADB 디바이스 조회가 시간 초과되었습니다.") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip() or "adb devices failed"
            raise RuntimeError(f"ADB 디바이스 조회 실패: {stderr}")

        devices: list[Dict[str, Any]] = []
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("List of devices attached"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            item: Dict[str, Any] = {
                "device_id": parts[0],
                "backend": "adb",
                "status": parts[1],
            }
            for token in parts[2:]:
                if ":" not in token:
                    continue
                key, value = token.split(":", 1)
                item[key] = value
            devices.append(item)
        return devices

    def _execute_adb_device_command_sync(
        self,
        device_id: str,
        command: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        if command == "screenshot":
            return self._adb_screenshot(device_id, args)
        if command == "install_apk":
            return self._adb_install_apk(device_id, args)
        if command == "list_apps":
            return self._adb_list_apps(device_id, args)
        if command == "tap":
            return self._adb_tap(device_id, args)
        if command == "swipe":
            return self._adb_swipe(device_id, args)
        if command == "input_text":
            return self._adb_input_text(device_id, args)
        if command == "get_device_info":
            return self._adb_get_device_info(device_id)
        raise ValueError(f"지원하지 않는 device_command: {command}")

    def _adb_screenshot(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = self._run_adb(device_id, "exec-out", "screencap", "-p", timeout=20)
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("screenshot", result))

        png_bytes = bytes(result.stdout or b"")
        if not png_bytes:
            raise ValueError("스크린샷 데이터를 받지 못했습니다.")

        path = self._store_screenshot_bytes(
            png_bytes,
            str(args.get("output_path") or "").strip(),
        )
        data: Dict[str, Any] = {
            "path": path,
            "mime": "image/png",
            "bytes": len(png_bytes),
        }
        if args.get("include_base64"):
            data["base64"] = base64.b64encode(png_bytes).decode("ascii")
        return {"status": "success", "data": data}

    def _adb_install_apk(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        apk_path = str(args.get("apk_path") or args.get("path") or "").strip()
        if not apk_path:
            raise ValueError("install_apk에는 args.apk_path가 필요합니다.")
        if not os.path.exists(apk_path):
            raise ValueError(f"APK 파일을 찾을 수 없습니다: {apk_path}")

        adb_args = ["install"]
        if bool(args.get("replace", True)):
            adb_args.append("-r")
        if bool(args.get("grant_permissions", False)):
            adb_args.append("-g")
        adb_args.append(apk_path)
        result = self._run_adb(device_id, *adb_args, timeout=180, text=True)
        if result.returncode != 0 or "Success" not in (result.stdout or ""):
            raise ValueError(self._format_adb_error("install_apk", result))

        return {
            "status": "success",
            "data": {
                "apk_path": apk_path,
                "stdout": (result.stdout or "").strip(),
                "replaced": bool(args.get("replace", True)),
            },
        }

    def _adb_list_apps(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        adb_args = ["shell", "pm", "list", "packages"]
        if bool(args.get("third_party_only", False)):
            adb_args.append("-3")
        result = self._run_adb(device_id, *adb_args, timeout=20, text=True)
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("list_apps", result))

        packages = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.append(line.split("package:", 1)[1].strip())

        return {
            "status": "success",
            "data": {
                "packages": packages,
                "count": len(packages),
                "third_party_only": bool(args.get("third_party_only", False)),
            },
        }

    def _adb_tap(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        x = self._coerce_int(args.get("x"))
        y = self._coerce_int(args.get("y"))
        if x is None or y is None:
            raise ValueError("tap에는 args.x, args.y가 필요합니다.")
        result = self._run_adb(
            device_id,
            "shell",
            "input",
            "tap",
            str(x),
            str(y),
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("tap", result))
        return {"status": "success", "data": {"x": x, "y": y}}

    def _adb_swipe(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        x1 = self._coerce_int(args.get("x1"))
        y1 = self._coerce_int(args.get("y1"))
        x2 = self._coerce_int(args.get("x2"))
        y2 = self._coerce_int(args.get("y2"))
        if None in (x1, y1, x2, y2):
            raise ValueError("swipe에는 args.x1, args.y1, args.x2, args.y2가 필요합니다.")
        duration_ms = self._coerce_int(
            args.get("duration_ms", args.get("duration", 400)),
            default=400,
        )
        result = self._run_adb(
            device_id,
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("swipe", result))
        return {
            "status": "success",
            "data": {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "duration_ms": duration_ms,
            },
        }

    def _adb_input_text(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        text = str(args.get("text") or "")
        if not text:
            raise ValueError("input_text에는 args.text가 필요합니다.")

        result = self._run_adb(
            device_id,
            "shell",
            "input",
            "text",
            self._escape_adb_text(text),
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("input_text", result))
        return {
            "status": "success",
            "data": {
                "text": text,
                "length": len(text),
            },
        }

    def _adb_get_device_info(self, device_id: str) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "device_id": device_id,
            "device_type": "android",
            "state": self._run_adb(
                device_id,
                "get-state",
                timeout=10,
                text=True,
            ).stdout.strip(),
        }

        for field, prop in _ANDROID_DEVICE_INFO_PROPS.items():
            value = self._run_adb(
                device_id,
                "shell",
                "getprop",
                prop,
                timeout=10,
                text=True,
            ).stdout.strip()
            if value:
                data[field] = value

        android_id = self._run_adb(
            device_id,
            "shell",
            "settings",
            "get",
            "secure",
            "android_id",
            timeout=10,
            text=True,
        ).stdout.strip()
        if android_id:
            data["android_id"] = android_id

        kernel = self._run_adb(
            device_id,
            "shell",
            "uname",
            "-a",
            timeout=10,
            text=True,
        ).stdout.strip()
        if kernel:
            data["kernel"] = kernel

        size = self._run_adb(
            device_id,
            "shell",
            "wm",
            "size",
            timeout=10,
            text=True,
        ).stdout.strip()
        if size:
            data["screen_size"] = size

        density = self._run_adb(
            device_id,
            "shell",
            "wm",
            "density",
            timeout=10,
            text=True,
        ).stdout.strip()
        if density:
            data["screen_density"] = density

        if isinstance(data.get("sdk_int"), str):
            data["sdk_int"] = self._coerce_int(data["sdk_int"])

        return {"status": "success", "data": data}

    def _run_adb(
        self,
        device_id: str,
        *args: str,
        timeout: int = 30,
        text: bool = False,
        serial: str | None = None,
    ) -> subprocess.CompletedProcess:
        adb_serial = serial or device_id
        cmd = ["adb", "-s", adb_serial, *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=text,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise ValueError("ADB가 설치되어 있지 않습니다.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"ADB 명령 시간 초과: {' '.join(args)}") from exc

    def _format_adb_error(self, command: str, result: subprocess.CompletedProcess) -> str:
        stdout = result.stdout.decode("utf-8", errors="ignore") if isinstance(result.stdout, bytes) else (result.stdout or "")
        stderr = result.stderr.decode("utf-8", errors="ignore") if isinstance(result.stderr, bytes) else (result.stderr or "")
        detail = stderr.strip() or stdout.strip() or f"returncode={result.returncode}"
        return f"{command} 실행 실패: {detail}"

    def _store_screenshot_bytes(self, image_bytes: bytes, output_path: str) -> str:
        if output_path:
            path = output_path
        else:
            fd, path = tempfile.mkstemp(prefix="aads-device-", suffix=".png")
            os.close(fd)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(image_bytes)
        return path

    def _normalize_screenshot_payload(
        self,
        data: Dict[str, Any],
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized = dict(data)
        image_b64 = str(normalized.pop("base64", "") or "")
        if not image_b64:
            return normalized
        try:
            image_bytes = base64.b64decode(image_b64, validate=True)
        except Exception:
            if args.get("include_base64"):
                normalized["base64"] = image_b64
            return normalized

        normalized["path"] = self._store_screenshot_bytes(
            image_bytes,
            str(args.get("output_path") or "").strip(),
        )
        normalized["bytes"] = int(normalized.get("bytes") or len(image_bytes))
        if args.get("include_base64"):
            normalized["base64"] = image_b64
        return normalized

    def _escape_adb_text(self, text: str) -> str:
        escaped: list[str] = []
        for ch in text:
            if ch == " ":
                escaped.append("%s")
            elif ch in "\\\"'&<>|;()$`":
                escaped.append("\\" + ch)
            else:
                escaped.append(ch)
        return "".join(escaped)

    def _coerce_int(self, value: Any, default: int | None = None) -> int | None:
        try:
            if value in (None, ""):
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _coerce_float(self, value: Any, default: float) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default


# 싱글톤 인스턴스 — hot-reload 시 기존 연결 상태 보존
import sys as _sys_mgr_reload
_prev_mgr_mod = _sys_mgr_reload.modules.get(__name__)
if _prev_mgr_mod is not None and hasattr(_prev_mgr_mod, 'pc_agent_manager') and isinstance(getattr(_prev_mgr_mod, 'pc_agent_manager'), PCAgentManager):
    pc_agent_manager = _prev_mgr_mod.pc_agent_manager
else:
    pc_agent_manager = PCAgentManager()
