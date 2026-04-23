"""
PTY 터미널 세션 API.
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.auth import get_current_user
from app.models.terminal import (
    TerminalExecuteRequest,
    TerminalInputOut,
    TerminalInputRequest,
    TerminalResizeRequest,
    TerminalSessionCreate,
    TerminalSessionOut,
)
from app.services.terminal_runner import terminal_runner

router = APIRouter()


def _user_id(current_user: dict) -> str:
    user_id = str(current_user.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user context")
    return user_id


def _format_sse(event: dict) -> str:
    event_type = str(event.get("type") or "message")
    seq = str(event.get("seq", ""))
    payload = json.dumps(event, ensure_ascii=False)
    return f"id: {seq}\nevent: {event_type}\ndata: {payload}\n\n"


@router.get("/terminal/sessions", response_model=List[TerminalSessionOut], tags=["terminal"])
async def list_terminal_sessions(current_user: dict = Depends(get_current_user)):
    """현재 사용자 터미널 세션 목록."""
    return terminal_runner.list_sessions(_user_id(current_user))


@router.post("/terminal/sessions", response_model=TerminalSessionOut, status_code=201, tags=["terminal"])
async def create_terminal_session(
    req: TerminalSessionCreate,
    current_user: dict = Depends(get_current_user),
):
    """새 PTY 터미널 세션 생성."""
    try:
        return await terminal_runner.create_session(
            user_id=_user_id(current_user),
            user_email=str(current_user.get("email") or ""),
            cwd=req.cwd,
            shell=req.shell,
            env=req.env,
            cols=req.cols,
            rows=req.rows,
            title=req.title,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/terminal/sessions/{session_id}", response_model=TerminalSessionOut, tags=["terminal"])
async def get_terminal_session(session_id: str, current_user: dict = Depends(get_current_user)):
    """터미널 세션 단건 조회."""
    try:
        return terminal_runner.get_session(session_id, _user_id(current_user))
    except KeyError:
        raise HTTPException(status_code=404, detail="terminal session not found")


@router.post("/terminal/sessions/{session_id}/input", response_model=TerminalInputOut, tags=["terminal"])
async def write_terminal_input(
    session_id: str,
    req: TerminalInputRequest,
    current_user: dict = Depends(get_current_user),
):
    """PTY stdin에 raw 입력 전송."""
    user_id = _user_id(current_user)
    try:
        bytes_written = await terminal_runner.write_input(session_id, user_id, req.data)
        session = terminal_runner.get_session(session_id, user_id)
        return TerminalInputOut(
            session_id=session_id,
            accepted=True,
            bytes_written=bytes_written,
            last_seq=session.last_seq,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="terminal session not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/terminal/sessions/{session_id}/execute", response_model=TerminalInputOut, tags=["terminal"])
async def execute_terminal_command(
    session_id: str,
    req: TerminalExecuteRequest,
    current_user: dict = Depends(get_current_user),
):
    """명령 1개를 개행 포함으로 실행."""
    user_id = _user_id(current_user)
    try:
        bytes_written = await terminal_runner.execute_command(session_id, user_id, req.command)
        session = terminal_runner.get_session(session_id, user_id)
        return TerminalInputOut(
            session_id=session_id,
            accepted=True,
            bytes_written=bytes_written,
            last_seq=session.last_seq,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="terminal session not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/terminal/sessions/{session_id}/resize", response_model=TerminalSessionOut, tags=["terminal"])
async def resize_terminal_session(
    session_id: str,
    req: TerminalResizeRequest,
    current_user: dict = Depends(get_current_user),
):
    """PTY 윈도우 크기 변경."""
    try:
        return await terminal_runner.resize(session_id, _user_id(current_user), req.cols, req.rows)
    except KeyError:
        raise HTTPException(status_code=404, detail="terminal session not found")


@router.post("/terminal/sessions/{session_id}/close", response_model=TerminalSessionOut, tags=["terminal"])
async def close_terminal_session(session_id: str, current_user: dict = Depends(get_current_user)):
    """터미널 세션 종료."""
    try:
        return await terminal_runner.close_session(session_id, _user_id(current_user))
    except KeyError:
        raise HTTPException(status_code=404, detail="terminal session not found")


@router.get("/terminal/sessions/{session_id}/stream", tags=["terminal"])
async def stream_terminal_session(
    session_id: str,
    request: Request,
    since_seq: Optional[int] = Query(default=None, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """SSE 스트림 — 최근 버퍼 replay 후 실시간 PTY 출력 전달."""
    user_id = _user_id(current_user)
    if since_seq is None:
        last_event_id = request.headers.get("Last-Event-ID", "").strip()
        if last_event_id.isdigit():
            since_seq = int(last_event_id)

    try:
        buffered_events = terminal_runner.buffered_events(session_id, user_id, since_seq)
        session_snapshot = terminal_runner.get_session(session_id, user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="terminal session not found")

    async def event_generator():
        for event in buffered_events:
            yield _format_sse(event)

        if session_snapshot.status != "running":
            return

        q = terminal_runner.subscribe(session_id, user_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                    yield _format_sse(event)
                    if event.get("type") == "exit":
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            terminal_runner.unsubscribe(session_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
