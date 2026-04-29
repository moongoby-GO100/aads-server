"""AADS Device SDK — base device agent client."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import websockets

from device_sdk.dispatcher import CommandDispatcher
from device_sdk.heartbeat import HeartbeatManager

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("config.json")
_BACKOFF_SEQUENCE = [5, 10, 20, 40, 60]


def _load_or_create_agent_id(config_path: Path) -> str:
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if cfg.get("agent_id"):
            return cfg["agent_id"]
    except Exception:
        pass

    new_id = str(uuid.uuid4())[:12]
    try:
        cfg: dict = {}
        if config_path.exists():
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        cfg["agent_id"] = new_id
        config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("agent_id 저장 실패: %s", e)
    return new_id


async def _send_heartbeat(ws: Any) -> None:
    await ws.send(json.dumps({
        "type": "heartbeat",
        "id": str(uuid.uuid4()),
        "payload": {},
    }))


class DeviceAgent:
    """Base class for PC and mobile device agents."""

    def __init__(
        self,
        server_url: str,
        token: str,
        agent_id: str | None = None,
        device_type: str = "pc",
    ) -> None:
        self.server_url = server_url
        self.token = token
        self.device_type = device_type
        self.agent_id = agent_id if agent_id else _load_or_create_agent_id(_CONFIG_PATH)
        self.dispatcher = CommandDispatcher()
        self._heartbeat = HeartbeatManager(interval=25.0)
        self._running = True

    async def run(self) -> None:
        logger.info("DeviceAgent 시작 agent_id=%s device_type=%s", self.agent_id, self.device_type)
        attempt = 0
        while self._running:
            try:
                await self._connect()
                attempt = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                delay = _BACKOFF_SEQUENCE[min(attempt, len(_BACKOFF_SEQUENCE) - 1)]
                logger.error("연결 오류: %s — %ds 후 재연결 (시도 %d)", e, delay, attempt + 1)
                attempt += 1
            if self._running:
                await asyncio.sleep(_BACKOFF_SEQUENCE[min(attempt - 1, len(_BACKOFF_SEQUENCE) - 1)])

    async def _connect(self) -> None:
        url = f"{self.server_url}/{self.agent_id}"
        if self.token:
            url = f"{url}?token={self.token}"

        logger.info("서버 연결 중: %s", url)

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as ws:
            logger.info("서버 연결 성공")

            await ws.send(json.dumps({
                "type": "register",
                "id": str(uuid.uuid4()),
                "payload": {
                    "agent_id": self.agent_id,
                    "device_type": self.device_type,
                    "capabilities": self.get_capabilities(),
                },
            }))

            await self._heartbeat.start(ws, _send_heartbeat)
            await self.on_connect()

            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("잘못된 JSON 수신: %s", str(raw)[:100])
                        continue
                    await self._on_message(ws, msg)
            finally:
                self._heartbeat.stop()
                await self.on_disconnect()

    async def _on_message(self, ws: Any, msg: dict) -> None:
        msg_type = msg.get("type", "")
        if msg_type == "command":
            asyncio.create_task(self._handle_command(ws, msg))
        elif msg_type == "heartbeat":
            pass
        else:
            logger.debug("알 수 없는 메시지 타입: %s", msg_type)

    async def _handle_command(self, ws: Any, msg: dict) -> None:
        command_id = msg.get("id", "")
        payload = msg.get("payload", {})
        command_type = payload.get("command_type", "")
        params = payload.get("params", {})

        logger.info("명령 수신 command_id=%s type=%s", command_id, command_type)

        result = await self.dispatcher.dispatch(command_type, params)

        try:
            await ws.send(json.dumps({
                "type": "result",
                "id": command_id,
                "payload": result,
            }))
            logger.info("결과 전송 command_id=%s status=%s", command_id, result.get("status"))
        except Exception as e:
            logger.error("결과 전송 실패 command_id=%s: %s", command_id, e)

    async def on_connect(self) -> None:
        """Called after a successful WebSocket connection. Override in subclasses."""

    async def on_disconnect(self) -> None:
        """Called after the WebSocket connection closes. Override in subclasses."""

    def get_capabilities(self) -> list[str]:
        return self.dispatcher.available_commands()

    def stop(self) -> None:
        self._running = False
