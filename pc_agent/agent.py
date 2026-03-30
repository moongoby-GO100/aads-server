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

# 명령 모듈 임포트 — COMMAND_HANDLERS만 사용 (개별 임포트 금지: _safe_import 방어 무력화)
from commands import COMMAND_HANDLERS

# updater는 자동업데이트 루프에서 직접 참조 필요 (방어적)
try:
    from commands import updater
except ImportError:
    updater = None  # type: ignore[assignment]

# screen_stream은 WebSocket 참조가 필요하므로 별도 임포트 (방어적)
try:
    from commands.screen_stream import get_streamer
except ImportError:
    get_streamer = None  # type: ignore[assignment]

# PyInstaller --windowed 환경: sys.stderr=None → StreamHandler 사용 불가
# FileHandler만 사용하여 깜박임 방지
_log_dir = os.path.join(
    os.environ.get("KAKAOBOT_INSTALL_DIR", os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local")),
        "KakaoBot"
    )),
    "logs",
)
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(os.path.join(_log_dir, "agent.log"), encoding="utf-8")],
)
logger = logging.getLogger("pc-agent")

# ── 설정 ─────────────────────────────────────────────────────────────────

SERVER_URL = os.getenv("AADS_SERVER_URL", "wss://aads.newtalk.kr/api/v1/pc-agent/ws")
AGENT_SECRET = os.getenv("AADS_AGENT_TOKEN", os.getenv("PC_AGENT_SECRET", ""))
HEARTBEAT_INTERVAL = 25  # 초
RECONNECT_DELAY = 5  # 초
AUTO_UPDATE_INTERVAL = 300  # 초 — 5분마다 서버 버전 확인 (HTTP 기반)


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

            # 하트비트 + 자동 업데이트 태스크 시작
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))
            update_task = asyncio.create_task(self._auto_update_loop(ws))

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
                update_task.cancel()

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

    async def _auto_update_loop(self, ws: Any) -> None:
        """1분마다 서버 업데이트 확인 → 변경 있으면 재다운로드 + 재시작."""
        if updater is None:
            logger.warning("updater 모듈 미로드 — 자동 업데이트 비활성화")
            return
        await asyncio.sleep(30)  # 시작 후 30초 대기
        while True:
            try:
                has_update = await updater.check_for_updates()
                if has_update:
                    logger.info("자동 업데이트 감지! git pull + 재시작 진행")
                    # 서버에 업데이트 알림
                    await ws.send(json.dumps({
                        "type": "status",
                        "id": str(uuid.uuid4()),
                        "payload": {"message": "자동 업데이트 감지, 재시작 중..."},
                    }))
                    await updater.execute({"force": True})
                    return  # 재시작되므로 여기까지 도달 안 함
            except Exception as e:
                logger.debug("자동 업데이트 확인 실패: %s", e)
            await asyncio.sleep(AUTO_UPDATE_INTERVAL)

    async def _handle_command(self, ws: Any, msg: Dict[str, Any]) -> None:
        """명령 실행 및 결과 반환."""
        command_id = msg.get("id", "")
        payload = msg.get("payload", {})
        command_type = payload.get("command_type", "")
        params = payload.get("params", {})

        logger.info("명령 수신 command_id=%s type=%s", command_id, command_type)

        # 스트리밍 명령은 WebSocket 참조가 필요하므로 직접 처리
        if command_type in ("stream_start", "stream_stop") and get_streamer is None:
            result = {"status": "error", "data": {"error": "screen_stream 모듈 미설치"}}
        elif command_type == "stream_start":
            try:
                streamer = get_streamer()
                await streamer.start(ws, params)
                result = {"status": "success", "data": {"message": "스트리밍 시작됨"}}
            except Exception as e:
                logger.error("스트리밍 시작 오류: %s", e)
                result = {"status": "error", "data": {"error": str(e)}}
        elif command_type == "stream_stop":
            try:
                streamer = get_streamer()
                await streamer.stop()
                result = {"status": "success", "data": {"message": "스트리밍 중지됨"}}
            except Exception as e:
                logger.error("스트리밍 중지 오류: %s", e)
                result = {"status": "error", "data": {"error": str(e)}}
        else:
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
        handler = COMMAND_HANDLERS.get(command_type)
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
