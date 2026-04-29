"""통합 디바이스 매니저 — PC/Android/iOS 에이전트 관리."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Set

from fastapi import WebSocket

from device_sdk.models import DeviceInfo, CommandResponse, WSMessage

logger = logging.getLogger(__name__)


class _DeviceConnection:
    def __init__(self, agent_id: str, websocket: WebSocket, info: DeviceInfo) -> None:
        self.agent_id = agent_id
        self.websocket = websocket
        self.info = info


class DeviceManager:
    def __init__(self) -> None:
        self._devices: Dict[str, _DeviceConnection] = {}
        self._pending_commands: Dict[str, asyncio.Event] = {}
        self._results: Dict[str, CommandResponse] = {}
        self._streaming_subscribers: Dict[str, Set[WebSocket]] = {}

    def register_device(
        self,
        agent_id: str,
        websocket: WebSocket,
        device_type: str = "pc",
        info: Dict[str, Any] | None = None,
    ) -> DeviceInfo:
        info = info or {}
        device_info = DeviceInfo(
            agent_id=agent_id,
            device_type=device_type,  # type: ignore[arg-type]
            hostname=info.get("hostname", ""),
            os_info=info.get("os_info", ""),
            capabilities=info.get("capabilities", []),
        )
        self._devices[agent_id] = _DeviceConnection(agent_id, websocket, device_info)
        logger.info("디바이스 등록: %s (%s)", agent_id, device_type)
        return device_info

    def unregister_device(self, agent_id: str) -> None:
        if agent_id in self._devices:
            del self._devices[agent_id]
            logger.info("디바이스 해제: %s", agent_id)

    def get_devices(self, device_type: str | None = None) -> list[dict[str, Any]]:
        result = []
        for conn in self._devices.values():
            if device_type and conn.info.device_type != device_type:
                continue
            result.append(conn.info.model_dump())
        return result

    def get_device(self, agent_id: str) -> DeviceInfo | None:
        conn = self._devices.get(agent_id)
        return conn.info if conn else None

    def get_device_capabilities(self, agent_id: str) -> list[str]:
        conn = self._devices.get(agent_id)
        return conn.info.capabilities if conn else []

    async def send_command(
        self,
        agent_id: str,
        command_type: str,
        params: Dict[str, Any],
        timeout: float = 30.0,
    ) -> CommandResponse:
        conn = self._devices.get(agent_id)
        if conn is None:
            if len(self._devices) == 1:
                conn = next(iter(self._devices.values()))
                agent_id = conn.agent_id
            else:
                return CommandResponse(
                    command_id="",
                    status="error",
                    data={"error": f"디바이스 미연결: {agent_id}"},
                )

        command_id = str(uuid.uuid4())
        self._pending_commands[command_id] = asyncio.Event()
        self._results[command_id] = CommandResponse(command_id=command_id)

        msg = WSMessage(
            type="command",
            id=command_id,
            payload={"command_type": command_type, "params": params},
        )
        try:
            await conn.websocket.send_json(msg.model_dump(mode="json"))
        except Exception as e:
            self._pending_commands.pop(command_id, None)
            self._results.pop(command_id, None)
            return CommandResponse(
                command_id=command_id,
                status="error",
                data={"error": f"전송 실패: {e}"},
            )

        event = self._pending_commands[command_id]
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            result = self._results.get(command_id)
            if result:
                result.status = "timeout"
                result.data = {"error": "응답 시간 초과"}
            self._pending_commands.pop(command_id, None)
            return result or CommandResponse(
                command_id=command_id, status="timeout"
            )

        self._pending_commands.pop(command_id, None)
        return self._results.pop(command_id, CommandResponse(command_id=command_id))

    def receive_result(self, command_id: str, result: Dict[str, Any]) -> None:
        stored = self._results.get(command_id)
        if stored is None:
            return
        stored.status = result.get("status", "success")  # type: ignore[assignment]
        stored.data = result.get("data")
        stored.completed_at = datetime.utcnow()
        event = self._pending_commands.get(command_id)
        if event:
            event.set()

    def update_heartbeat(self, agent_id: str) -> None:
        conn = self._devices.get(agent_id)
        if conn:
            conn.info.connected_at = datetime.utcnow()

    def add_stream_subscriber(self, agent_id: str, ws: WebSocket) -> None:
        if agent_id not in self._streaming_subscribers:
            self._streaming_subscribers[agent_id] = set()
        self._streaming_subscribers[agent_id].add(ws)

    def remove_stream_subscriber(self, agent_id: str, ws: WebSocket) -> int:
        subs = self._streaming_subscribers.get(agent_id)
        if subs:
            subs.discard(ws)
            remaining = len(subs)
            if remaining == 0:
                del self._streaming_subscribers[agent_id]
            return remaining
        return 0

    async def broadcast_frame(self, agent_id: str, frame_data: str) -> None:
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
        if dead and not subs:
            del self._streaming_subscribers[agent_id]


device_manager = DeviceManager()
