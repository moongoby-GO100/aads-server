"""
AADS-195: PC 제어 에이전트 API.
WebSocket 엔드포인트 + REST API.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.models.pc_agent import CommandRequest, WSMessage
from app.services.pc_agent_manager import pc_agent_manager

logger = logging.getLogger(__name__)
router = APIRouter()

PC_AGENT_SECRET = os.environ.get("PC_AGENT_SECRET", "")
HEARTBEAT_INTERVAL = 30  # 초


# ── WebSocket ──────────────────────────────────────────────────────────

@router.websocket("/pc-agent/ws/{agent_id}")
async def ws_pc_agent(websocket: WebSocket, agent_id: str, token: str = Query("")):
    """PC 에이전트 WebSocket 연결."""
    # 인증
    if not PC_AGENT_SECRET or token != PC_AGENT_SECRET:
        await websocket.close(code=4001, reason="unauthorized")
        logger.warning("pc_agent_ws_auth_failed agent_id=%s", agent_id)
        return

    await websocket.accept()
    logger.info("pc_agent_ws_connected agent_id=%s", agent_id)

    # 등록 메시지 대기 (첫 메시지)
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        msg = WSMessage.model_validate(raw)
        if msg.type != "register":
            await websocket.close(code=4002, reason="first message must be register")
            return
        pc_agent_manager.register_agent(agent_id, websocket, msg.payload)
    except (asyncio.TimeoutError, Exception) as exc:
        logger.error("pc_agent_ws_register_failed agent_id=%s err=%s", agent_id, exc)
        await websocket.close(code=4003, reason="register failed")
        return

    # 메시지 수신 루프
    try:
        while True:
            raw = await asyncio.wait_for(
                websocket.receive_json(), timeout=HEARTBEAT_INTERVAL * 2
            )
            msg = WSMessage.model_validate(raw)

            if msg.type == "heartbeat":
                pc_agent_manager.update_heartbeat(agent_id)
                # pong 응답
                await websocket.send_json(
                    {"type": "heartbeat", "id": msg.id, "payload": {}}
                )

            elif msg.type == "result":
                pc_agent_manager.receive_result(msg.id, msg.payload)

            else:
                logger.warning(
                    "pc_agent_ws_unknown_type agent_id=%s type=%s", agent_id, msg.type
                )

    except (WebSocketDisconnect, asyncio.TimeoutError):
        logger.info("pc_agent_ws_disconnected agent_id=%s", agent_id)
    except Exception as exc:
        logger.error("pc_agent_ws_error agent_id=%s err=%s", agent_id, exc)
    finally:
        pc_agent_manager.unregister_agent(agent_id)


# ── REST API ──────────────────────────────────────────────────────────

@router.get("/pc-agent/agents")
async def list_agents():
    """연결된 에이전트 목록 조회."""
    agents = pc_agent_manager.list_agents()
    return {"agents": [a.model_dump(mode="json") for a in agents]}


@router.post("/pc-agent/execute")
async def execute_command(req: CommandRequest):
    """에이전트에 명령 실행 요청."""
    agent = pc_agent_manager.get_agent(req.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 '{req.agent_id}'가 연결되어 있지 않습니다.")

    try:
        command_id = await pc_agent_manager.send_command(
            req.agent_id, req.command_type, req.params
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"command_id": command_id, "status": "pending"}


@router.get("/pc-agent/result/{command_id}")
async def get_result(command_id: str, timeout: float = Query(30.0, ge=1.0, le=120.0)):
    """명령 실행 결과 조회 (대기)."""
    try:
        result = await pc_agent_manager.get_result(command_id, timeout=timeout)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return result.model_dump(mode="json")
