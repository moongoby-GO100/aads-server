"""
AADS Terminal Runner API models.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class TerminalSessionCreate(BaseModel):
    """PTY 터미널 세션 생성 요청."""
    cwd: str = Field(default="/root", max_length=4096)
    shell: str = Field(default="/bin/bash", max_length=512)
    title: Optional[str] = Field(default=None, max_length=200)
    env: Dict[str, str] = Field(default_factory=dict)
    cols: int = Field(default=120, ge=40, le=400)
    rows: int = Field(default=32, ge=10, le=200)

    @field_validator("cwd", "shell")
    @classmethod
    def strip_path_fields(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("field cannot be empty")
        return value


class TerminalInputRequest(BaseModel):
    """PTY stdin 입력."""
    data: str = Field(..., min_length=1, max_length=32768)


class TerminalExecuteRequest(BaseModel):
    """명령 실행 편의 요청."""
    command: str = Field(..., min_length=1, max_length=8192)


class TerminalResizeRequest(BaseModel):
    """PTY 윈도우 크기 변경."""
    cols: int = Field(..., ge=40, le=400)
    rows: int = Field(..., ge=10, le=200)


class TerminalSessionOut(BaseModel):
    """터미널 세션 상태."""
    session_id: str
    title: str
    shell: str
    cwd: str
    status: Literal["running", "exited"]
    backend_mode: Literal["pty", "pipe"]
    pid: Optional[int] = None
    returncode: Optional[int] = None
    cols: int
    rows: int
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    last_seq: int = 0
    output_bytes: int = 0
    recent_commands: List[str] = Field(default_factory=list)


class TerminalInputOut(BaseModel):
    """입력/명령 실행 응답."""
    session_id: str
    accepted: bool = True
    bytes_written: int
    last_seq: int
