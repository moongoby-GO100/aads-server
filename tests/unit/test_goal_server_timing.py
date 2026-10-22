"""/api/v1/goals 경로에만 Server-Timing 헤더와 느린 요청 로그가 붙는다."""
import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient

from app.core.server_timing import GoalServerTimingMiddleware, StepTimer, is_goal_path


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(GoalServerTimingMiddleware)

    @app.get("/api/v1/goals/{gid}/board")
    async def board(gid: str):
        timer = StepTimer()
        await asyncio.sleep(0.03)
        timer.lap("goal")
        return JSONResponse({"ok": True})

    @app.get("/api/v1/goals/{gid}/stream")
    async def stream(gid: str):
        async def gen():
            yield b"data: 1\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/v1/goals/{gid}/boom")
    async def boom(gid: str):
        raise RuntimeError("x")

    @app.get("/api/v1/other")
    async def other():
        return JSONResponse({"ok": True})

    return app


async def _get(app: FastAPI, path: str):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


def test_is_goal_path():
    assert is_goal_path("/api/v1/goals")
    assert is_goal_path("/api/v1/goals/x/board")
    assert not is_goal_path("/api/v1/goals-other")
    assert not is_goal_path("/api/v1/other")


@pytest.mark.asyncio
async def test_goal_path_has_server_timing_with_steps():
    r = await _get(_app(), "/api/v1/goals/g1/board")
    assert r.status_code == 200
    header = r.headers["server-timing"]
    assert header.startswith("app;dur=")
    assert "goal;dur=" in header
    app_ms = float(header.split("app;dur=")[1].split(",")[0])
    assert app_ms >= 25


@pytest.mark.asyncio
async def test_other_path_untouched():
    r = await _get(_app(), "/api/v1/other")
    assert "server-timing" not in r.headers


@pytest.mark.asyncio
async def test_streaming_response_has_no_header():
    r = await _get(_app(), "/api/v1/goals/g1/stream")
    assert r.status_code == 200
    assert "server-timing" not in r.headers


@pytest.mark.asyncio
async def test_slow_request_logged_over_threshold(monkeypatch, caplog):
    monkeypatch.setenv("GOAL_SLOW_REQUEST_MS", "10")
    with caplog.at_level(logging.WARNING, logger="app.core.server_timing"):
        await _get(_app(), "/api/v1/goals/g1/board")
    msgs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("slow_request")]
    assert len(msgs) == 1
    assert "path=/api/v1/goals/g1/board" in msgs[0]
    assert "method=GET" in msgs[0]
    assert "status=200" in msgs[0]
    assert "dur_ms=" in msgs[0]


@pytest.mark.asyncio
async def test_fast_request_not_logged(monkeypatch, caplog):
    monkeypatch.setenv("GOAL_SLOW_REQUEST_MS", "5000")
    with caplog.at_level(logging.WARNING, logger="app.core.server_timing"):
        await _get(_app(), "/api/v1/goals/g1/board")
    assert not [r for r in caplog.records if r.getMessage().startswith("slow_request")]


@pytest.mark.asyncio
async def test_unhandled_exception_logged_as_500(monkeypatch, caplog):
    monkeypatch.setenv("GOAL_SLOW_REQUEST_MS", "0")
    with caplog.at_level(logging.WARNING, logger="app.core.server_timing"):
        await _get(_app(), "/api/v1/goals/g1/boom")
    msgs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("slow_request")]
    assert len(msgs) == 1 and "status=500" in msgs[0]


def test_step_timer_without_context_is_noop():
    StepTimer().lap("x")
