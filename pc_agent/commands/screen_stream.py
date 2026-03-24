"""
AADS-195: 실시간 화면 스트리밍 모듈.
fps 간격으로 스크린샷 캡처 → base64 JPEG → WebSocket 전송.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger("pc-agent.screen_stream")


class ScreenStreamer:
    """실시간 화면 스트리밍."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._fps: int = 2
        self._quality: int = 50
        self._scale: float = 0.5
        self._monitor: int = -1

    @property
    def is_streaming(self) -> bool:
        return self._running

    async def start(self, ws: Any, config: Dict[str, Any]) -> None:
        """스트리밍 시작."""
        # 이미 실행 중이면 중지 후 재시작
        if self._running:
            await self.stop()

        self._fps = config.get("fps", 2)
        self._quality = config.get("quality", 50)
        self._scale = config.get("scale", 0.5)
        self._monitor = config.get("monitor", -1)
        self._running = True
        self._task = asyncio.create_task(self._capture_loop(ws))
        logger.info(
            "스트리밍 시작 fps=%d quality=%d scale=%.2f monitor=%d",
            self._fps, self._quality, self._scale, self._monitor,
        )

    async def stop(self) -> None:
        """스트리밍 중지."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("스트리밍 중지")

    async def _capture_loop(self, ws: Any) -> None:
        """fps 간격으로 스크린샷 캡처 + WebSocket 전송."""
        import pyautogui
        from PIL import Image

        interval = 1.0 / self._fps

        while self._running:
            try:
                # 스크린샷 캡처
                if self._monitor == -1:
                    # 전체 화면
                    img = pyautogui.screenshot()
                else:
                    # 개별 모니터 — screeninfo로 모니터 영역 가져오기
                    try:
                        from screeninfo import get_monitors
                        monitors = get_monitors()
                        if 0 <= self._monitor < len(monitors):
                            m = monitors[self._monitor]
                            img = pyautogui.screenshot(region=(m.x, m.y, m.width, m.height))
                        else:
                            img = pyautogui.screenshot()
                    except ImportError:
                        # screeninfo 없으면 전체 화면 폴백
                        img = pyautogui.screenshot()

                # 리사이즈
                if self._scale < 1.0:
                    new_w = int(img.width * self._scale)
                    new_h = int(img.height * self._scale)
                    img = img.resize((new_w, new_h), Image.LANCZOS)

                # JPEG 인코딩
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self._quality)
                frame_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

                # WebSocket 전송
                msg = json.dumps({
                    "type": "stream_frame",
                    "id": str(uuid.uuid4()),
                    "payload": {"frame": frame_b64},
                })
                await ws.send(msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("프레임 캡처 실패: %s", e)

            await asyncio.sleep(interval)


# 싱글톤 인스턴스
_streamer = ScreenStreamer()


async def stream_start(params: Dict[str, Any]) -> Dict[str, Any]:
    """stream_start 명령 핸들러 — agent.py에서 ws를 주입해야 함."""
    # 이 함수는 직접 호출되지 않음 (ws 필요). agent.py에서 _streamer.start() 직접 호출.
    return {"status": "error", "data": {"error": "stream_start는 agent에서 직접 처리"}}


async def stream_stop(params: Dict[str, Any]) -> Dict[str, Any]:
    """stream_stop 명령 핸들러."""
    await _streamer.stop()
    return {"status": "success", "data": {"message": "스트리밍 중지됨"}}


def get_streamer() -> ScreenStreamer:
    """싱글톤 ScreenStreamer 인스턴스 반환."""
    return _streamer
