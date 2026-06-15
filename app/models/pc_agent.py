"""
AADS-195: PC 제어 에이전트 — Pydantic 모델
WebSocket 기반 원격 PC 제어 요청/응답 스키마.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class AgentInfo(BaseModel):
    """연결된 PC 에이전트 정보."""
    agent_id: str
    hostname: str = ""
    os_info: str = ""
    capabilities: list[str] = Field(default_factory=list)
    command_types: list[str] = Field(default_factory=list)
    connected_at: datetime = Field(default_factory=datetime.utcnow)
    last_heartbeat: datetime = Field(default_factory=datetime.utcnow)


class CommandRequest(BaseModel):
    """명령 실행 요청."""
    agent_id: str
    command_type: str  # PC Agent COMMAND_HANDLERS에 등록된 모든 명령 허용
    params: Dict[str, Any] = Field(default_factory=dict)


class RoutedCommandRequest(BaseModel):
    """Capability 라우팅 기반 명령 실행 요청."""
    command_type: str
    params: Dict[str, Any] = Field(default_factory=dict)
    agent_id: str = ""
    job_type: str = "general"
    required_capabilities: list[str] = Field(default_factory=list)
    queue_if_busy: bool = True
    wait_for_turn: bool = True
    queue_wait_timeout_seconds: float = Field(default=120.0, ge=1.0, le=900.0)
    lease_ttl_seconds: int = Field(default=180, ge=30, le=1800)
    command_timeout_seconds: float = Field(default=120.0, ge=1.0, le=900.0)

    @field_validator("command_type")
    @classmethod
    def _command_type_required(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("command_type은 필수입니다.")
        return value

    @field_validator("job_type")
    @classmethod
    def _normalize_job_type(cls, value: str) -> str:
        value = (value or "general").strip().lower().replace("-", "_")
        return value or "general"


class CommandResult(BaseModel):
    """명령 실행 결과."""
    command_id: str
    agent_id: str
    status: Literal["pending", "success", "error", "timeout"] = "pending"
    result: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class StreamConfig(BaseModel):
    """화면 스트리밍 설정."""
    fps: int = Field(default=2, ge=1, le=5)
    quality: int = Field(default=50, ge=10, le=95)
    scale: float = Field(default=0.5, ge=0.25, le=1.0)
    monitor: int = Field(default=-1)  # -1 = 전체, 0/1 = 개별 모니터


class WSMessage(BaseModel):
    """WebSocket 메시지 포맷."""
    type: str  # command, result, heartbeat, register, stream_start, stream_stop, stream_frame
    id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
