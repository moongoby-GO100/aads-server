"""
AADS-195: PC 제어 에이전트 매니저 (싱글톤).
WebSocket 연결 관리, 명령 전송/결과 수신.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket

from app.models.pc_agent import AgentInfo, CommandResult, StreamConfig, WSMessage

logger = logging.getLogger(__name__)


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

    # ── 에이전트 등록/해제 ──────────────────────────────────────────

    def register_agent(
        self, agent_id: str, websocket: WebSocket, info: Dict[str, Any]
    ) -> AgentInfo:
        """에이전트 등록."""
        agent_info = AgentInfo(
            agent_id=agent_id,
            hostname=info.get("hostname", ""),
            os_info=info.get("os_info", ""),
        )
        self._agents[agent_id] = _AgentConnection(agent_id, websocket, agent_info)
        logger.info("pc_agent_registered agent_id=%s hostname=%s", agent_id, agent_info.hostname)
        return agent_info

    def unregister_agent(self, agent_id: str) -> None:
        """에이전트 해제."""
        if agent_id in self._agents:
            del self._agents[agent_id]
            logger.info("pc_agent_unregistered agent_id=%s", agent_id)

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

    # ── 조회 ──────────────────────────────────────────────────────

    def list_agents(self) -> list[AgentInfo]:
        """연결된 에이전트 목록."""
        return [conn.info for conn in self._agents.values()]

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """특정 에이전트 정보 조회."""
        conn = self._agents.get(agent_id)
        return conn.info if conn else None


# 싱글톤 인스턴스
pc_agent_manager = PCAgentManager()
