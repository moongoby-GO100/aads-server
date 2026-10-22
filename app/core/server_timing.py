from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)

GOAL_PATH = "/api/v1/goals"
_DEFAULT_SLOW_MS = 500.0

_steps: ContextVar[Optional[dict[str, float]]] = ContextVar("server_timing_steps", default=None)


def slow_threshold_ms() -> float:
    try:
        return float(os.getenv("GOAL_SLOW_REQUEST_MS", str(_DEFAULT_SLOW_MS)))
    except ValueError:
        return _DEFAULT_SLOW_MS


def is_goal_path(path: str) -> bool:
    return path == GOAL_PATH or path.startswith(GOAL_PATH + "/")


class StepTimer:
    """직전 lap 이후 경과 시간을 단계 이름으로 요청 컨텍스트에 쌓는다. 컨텍스트가 없으면 아무것도 안 한다."""

    def __init__(self) -> None:
        self._last = time.perf_counter()

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        bucket = _steps.get()
        if bucket is not None:
            bucket[name] = bucket.get(name, 0.0) + (now - self._last) * 1000.0
        self._last = now


def format_server_timing(total_ms: float, steps: dict[str, float]) -> str:
    parts = [f"app;dur={total_ms:.1f}"]
    parts.extend(f"{name};dur={dur:.1f}" for name, dur in steps.items())
    return ", ".join(parts)


class GoalServerTimingMiddleware:
    """`/api/v1/goals` 경로에만 Server-Timing 헤더와 느린 요청 로그를 붙이는 순수 ASGI 미들웨어.

    스트리밍(SSE 등) 응답은 본문을 건드리지 않고 헤더도 생략한다.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_goal_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        steps: dict[str, float] = {}
        token = _steps.set(steps)
        status = {"code": 500, "started": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and not status["started"]:
                status["started"] = True
                status["code"] = message["status"]
                total_ms = (time.perf_counter() - start) * 1000.0
                headers = list(message.get("headers") or [])
                names = {k.lower(): v for k, v in headers}
                streaming = (
                    b"text/event-stream" in names.get(b"content-type", b"")
                    or b"content-length" not in names
                )
                if not streaming:
                    headers.append((b"server-timing", format_server_timing(total_ms, steps).encode("latin-1")))
                    message = {**message, "headers": headers}
                    self._log_if_slow(scope, status["code"], total_ms)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not status["started"]:
                self._log_if_slow(scope, 500, (time.perf_counter() - start) * 1000.0)
            raise
        finally:
            _steps.reset(token)

    @staticmethod
    def _log_if_slow(scope, status_code: int, dur_ms: float) -> None:
        if dur_ms > slow_threshold_ms():
            logger.warning(
                "slow_request path=%s method=%s status=%s dur_ms=%.1f",
                scope.get("path", ""), scope.get("method", ""), status_code, dur_ms,
            )
