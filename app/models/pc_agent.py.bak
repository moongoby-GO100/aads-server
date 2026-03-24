"""
AADS-195: PC 제어 에이전트 — Pydantic 모델
WebSocket 기반 원격 PC 제어 요청/응답 스키마.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    """연결된 PC 에이전트 정보."""
    agent_id: str
    hostname: str = ""
    os_info: str = ""
    connected_at: datetime = Field(default_factory=datetime.utcnow)
    last_heartbeat: datetime = Field(default_factory=datetime.utcnow)


class CommandRequest(BaseModel):
    """명령 실행 요청."""
    agent_id: str
    command_type: str  # PC Agent COMMAND_HANDLERS에 등록된 모든 명령 허용
    params: Dict[str, Any] = Field(default_factory=dict)


class CommandResult(BaseModel):
    """명령 실행 결과."""
    command_id: str
    agent_id: str
    status: Literal["pending", "success", "error", "timeout"] = "pending"
    result: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class WSMessage(BaseModel):
    """WebSocket 메시지 포맷."""
    type: Literal["command", "result", "heartbeat", "register"]
    id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
