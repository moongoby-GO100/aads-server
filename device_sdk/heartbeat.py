"""AADS Device SDK — heartbeat manager."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class HeartbeatManager:
    def __init__(self, interval: float = 25.0) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._send_func: Callable | None = None

    async def start(self, ws: Any, send_func: Callable) -> None:
        self._ws = ws
        self._send_func = send_func
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._send_func(self._ws)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("하트비트 전송 실패: %s", e)
                break
