"""
AADS-195: PC 제어 에이전트 — Windows 클라이언트.
WebSocket으로 AADS 서버에 연결, 명령 수신/실행/결과 반환.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
import uuid
from typing import Any, Dict

import websockets

# 명령 모듈 임포트
from commands import shell, screenshot, file_ops, process, system_info, kakao

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pc-agent")

# ── 설정 ─────────────────────────────────────────────────────────────────

SERVER_URL = os.getenv("AADS_SERVER_URL", "wss://aads.newtalk.kr/api/v1/pc-agent/ws")
AGENT_SECRET = os.getenv("PC_AGENT_SECRET", "")
HEARTBEAT_INTERVAL = 25  # 초
RECONNECT_DELAY = 5  # 초


class PCAgent:
    """PC 제어 에이전트 클라이언트."""

    def __init__(self) -> None:
        self.agent_id = str(uuid.uuid4())[:12]
        self.hostname = platform.node()
        self.os_info = f"{platform.system()} {platform.release()} {platform.version()}"
        self._running = True

    async def run(self) -> None:
        """메인 루프 — 서버 연결 + 재연결."""
        logger.info("PC Agent 시작 agent_id=%s hostname=%s", self.agent_id, self.hostname)

        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.error("연결 오류: %s — %d초 후 재연결", e, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

    async def _connect(self) -> None:
        """WebSocket 서버 연결."""
        url = f"{SERVER_URL}/{self.agent_id}"
        if AGENT_SECRET:
            url = f"{url}?token={AGENT_SECRET}"

        logger.info("서버 연결 중: %s", url)

        async with websockets.connect(url) as ws:
            logger.info("서버 연결 성공")

            # 등록 메시지 전송
            await ws.send(json.dumps({
                "type": "register",
                "id": str(uuid.uuid4()),
                "payload": {
                    "hostname": self.hostname,
                    "os_info": self.os_info,
                },
            }))

            # 하트비트 태스크 시작
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))

            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    msg_type = msg.get("type", "")

                    if msg_type == "command":
                        asyncio.create_task(self._handle_command(ws, msg))
                    elif msg_type == "heartbeat":
                        pass  # 서버 ACK
                    else:
                        logger.debug("알 수 없는 메시지: %s", msg_type)
            finally:
                heartbeat_task.cancel()

    async def _heartbeat(self, ws: Any) -> None:
        """주기적 하트비트 전송."""
        while True:
            try:
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "id": str(uuid.uuid4()),
                    "payload": {},
                }))
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except Exception:
                break

    async def _handle_command(self, ws: Any, msg: Dict[str, Any]) -> None:
        """명령 실행 및 결과 반환."""
        command_id = msg.get("id", "")
        payload = msg.get("payload", {})
        command_type = payload.get("command_type", "")
        params = payload.get("params", {})

        logger.info("명령 수신 command_id=%s type=%s", command_id, command_type)

        try:
            result = await self._execute_command(command_type, params)
        except Exception as e:
            logger.error("명령 실행 오류 command_id=%s: %s", command_id, e)
            result = {"status": "error", "data": {"error": str(e)}}

        # 결과 전송
        await ws.send(json.dumps({
            "type": "result",
            "id": command_id,
            "payload": result,
        }))
        logger.info("결과 전송 command_id=%s status=%s", command_id, result.get("status"))

    async def _execute_command(self, command_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """명령 타입에 따른 실행 디스패치."""
        dispatch = {
            "shell": shell.execute,
            "screenshot": screenshot.execute,
            "file_list": file_ops.file_list,
            "file_read": file_ops.file_read,
            "file_write": file_ops.file_write,
            "process_list": process.execute,
            "system_info": system_info.execute,
            "kakao_send": kakao.kakao_send,
            "kakao_read": kakao.kakao_read,
        }

        handler = dispatch.get(command_type)
        if handler is None:
            return {"status": "error", "data": {"error": f"지원하지 않는 명령: {command_type}"}}

        return await handler(params)

    def stop(self) -> None:
        """에이전트 종료."""
        self._running = False
        logger.info("PC Agent 종료 요청")


def main() -> None:
    """엔트리포인트."""
    agent = PCAgent()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        agent.stop()
        logger.info("PC Agent 종료")


if __name__ == "__main__":
    main()
