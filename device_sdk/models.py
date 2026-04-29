"""AADS Device SDK — shared Pydantic models."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DeviceInfo(BaseModel):
    agent_id: str
    device_type: Literal["pc", "android", "ios"]
    hostname: str
    os_info: str
    capabilities: list[str]
    connected_at: datetime = Field(default_factory=datetime.utcnow)


class CommandRequest(BaseModel):
    command_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    command_type: str
    params: dict[str, Any] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    command_id: str
    status: Literal["success", "error", "timeout"]
    data: dict[str, Any] | None = None
    completed_at: datetime | None = None


class WSMessage(BaseModel):
    type: str
    id: str
    payload: dict[str, Any] = Field(default_factory=dict)
