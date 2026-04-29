"""통합 디바이스 API — PC/Android/iOS 에이전트 WebSocket + REST."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.services.device_manager import device_manager

logger = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_TIMEOUT = 50.0


async def _verify_token(token: str) -> bool:
    if not token:
        return False
    expected = os.environ.get("PC_AGENT_TOKEN", "")
    if expected and token == expected:
        return True
    try:
        from app.core.database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM api_tokens WHERE token=$1 AND is_active=true", token
            )
            return row is not None
    except Exception:
        return bool(expected and token == expected)


@router.websocket("/devices/ws/{agent_id}")
async def ws_device(
    websocket: WebSocket,
    agent_id: str,
    token: str = Query(""),
    device_type: str = Query("pc"),
):
    if not await _verify_token(token):
        await websocket.close(code=4001, reason="인증 실패")
        return

    await websocket.accept()
    device_info = None

    try:
        raw = await websocket.receive_json()
        if raw.get("type") != "register":
            await websocket.close(code=4002, reason="첫 메시지는 register여야 합니다")
            return

        payload = raw.get("payload", {})
        actual_type = payload.get("device_type", device_type)
        device_info = device_manager.register_device(
            agent_id, websocket, actual_type, payload
        )

        await websocket.send_json({"type": "registered", "payload": {"agent_id": agent_id}})

        while True:
            import asyncio
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(), timeout=HEARTBEAT_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("디바이스 %s 하트비트 타임아웃", agent_id)
                break

            msg_type = data.get("type", "")

            if msg_type == "heartbeat":
                device_manager.update_heartbeat(agent_id)
                await websocket.send_json({"type": "heartbeat", "id": data.get("id", "")})

            elif msg_type == "result":
                command_id = data.get("id", "")
                device_manager.receive_result(command_id, data.get("payload", {}))

            elif msg_type == "stream_frame":
                await device_manager.broadcast_frame(
                    agent_id, data.get("payload", {}).get("frame", "")
                )

            elif msg_type == "network_info":
                pass

    except WebSocketDisconnect:
        logger.info("디바이스 %s 연결 종료", agent_id)
    except Exception:
        logger.exception("디바이스 %s WebSocket 오류", agent_id)
    finally:
        device_manager.unregister_device(agent_id)


class CommandRequest(BaseModel):
    agent_id: str = ""
    command_type: str
    params: dict[str, Any] = {}
    timeout: float = 30.0


@router.get("/devices")
async def list_devices(device_type: str = Query(None)):
    devices = device_manager.get_devices(device_type)
    return {"devices": devices, "count": len(devices)}


@router.post("/devices/execute")
async def execute_command(req: CommandRequest):
    result = await device_manager.send_command(
        req.agent_id, req.command_type, req.params, req.timeout
    )
    return result.model_dump()


@router.get("/devices/{agent_id}/status")
async def device_status(agent_id: str):
    info = device_manager.get_device(agent_id)
    if info is None:
        return {"status": "disconnected", "agent_id": agent_id}
    return {"status": "connected", **info.model_dump()}


@router.get("/devices/{agent_id}/capabilities")
async def device_capabilities(agent_id: str):
    caps = device_manager.get_device_capabilities(agent_id)
    return {"agent_id": agent_id, "capabilities": caps}
