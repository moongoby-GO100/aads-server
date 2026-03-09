"""
AADS-170: CEO Chat-First 시스템 — Pydantic 모델
chat_workspaces / chat_sessions / chat_messages / chat_artifacts /
chat_drive_files / research_archive 에 대응하는 요청/응답 스키마.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ─── Workspace ───────────────────────────────────────────────────────────────

class WorkspaceCreate(BaseModel):
    name: str = Field(..., max_length=100)
    system_prompt: Optional[str] = None
    files: List[Any] = Field(default_factory=list)
    settings: Dict[str, Any] = Field(default_factory=dict)
    color: str = Field(default="#6366F1", max_length=7)
    icon: str = Field(default="💬", max_length=10)


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    system_prompt: Optional[str] = None
    files: Optional[List[Any]] = None
    settings: Optional[Dict[str, Any]] = None
    color: Optional[str] = Field(None, max_length=7)
    icon: Optional[str] = Field(None, max_length=10)


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    system_prompt: Optional[str]
    files: List[Any]
    settings: Dict[str, Any]
    color: str
    icon: str
    created_at: datetime
    updated_at: datetime


# ─── Session ─────────────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    workspace_id: uuid.UUID
    title: Optional[str] = Field(None, max_length=200)


class SessionUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    pinned: Optional[bool] = None
    summary: Optional[str] = None


class SessionOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: Optional[str]
    summary: Optional[str]
    message_count: int
    cost_total: Decimal
    pinned: bool
    created_at: datetime
    updated_at: datetime


# ─── Message ─────────────────────────────────────────────────────────────────

class MessageSendRequest(BaseModel):
    session_id: uuid.UUID
    content: str
    attachments: List[Any] = Field(default_factory=list)
    model_override: Optional[str] = None


class MessageOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    model_used: Optional[str]
    intent: Optional[str]
    cost: Decimal
    tokens_in: int
    tokens_out: int
    bookmarked: bool
    attachments: List[Any]
    sources: List[Any]
    artifact_id: Optional[uuid.UUID]
    created_at: datetime


class MessageSearchOut(BaseModel):
    messages: List[MessageOut]
    total: int


# ─── AADS-188D: Diff 승인 ────────────────────────────────────────────────────

class ApproveDiffRequest(BaseModel):
    """코드 수정 diff 승인/거부 (Monaco DiffEditor UI → API)."""
    session_id: uuid.UUID
    tool_use_id: str = Field(..., min_length=1)
    action: str = Field(..., description="approve | reject")


class ApproveDiffOut(BaseModel):
    success: bool
    action: str
    message: Optional[str] = None


# ─── Artifact ────────────────────────────────────────────────────────────────

class ArtifactUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ArtifactOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    type: str
    title: Optional[str]
    content: str
    metadata: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ArtifactExportRequest(BaseModel):
    format: str = Field(..., description="pdf | md | html")


# ─── Drive ───────────────────────────────────────────────────────────────────

class DriveFileOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    filename: str
    file_path: str
    file_type: Optional[str]
    file_size: int
    uploaded_by: str
    metadata: Dict[str, Any]
    created_at: datetime


# ─── Research Archive ────────────────────────────────────────────────────────

class ResearchOut(BaseModel):
    id: uuid.UUID
    topic: str
    query: str
    sources: List[Any]
    summary: str
    full_report: Optional[str]
    model_used: Optional[str]
    cost: Optional[Decimal]
    session_id: Optional[uuid.UUID]
    created_at: datetime
