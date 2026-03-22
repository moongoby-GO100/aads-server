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

from pydantic import BaseModel, ConfigDict, Field


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
    tags: Optional[List[str]] = None


class SessionOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: Optional[str]
    summary: Optional[str]
    message_count: int
    cost_total: Decimal
    pinned: bool
    tags: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# ─── Message ─────────────────────────────────────────────────────────────────

class MessageSendRequest(BaseModel):
    session_id: uuid.UUID
    content: str
    attachments: List[Any] = Field(default_factory=list)
    model_override: Optional[str] = None
    reply_to_id: Optional[uuid.UUID] = None


class MessageUpdateRequest(BaseModel):
    content: str = Field(..., min_length=1)


class BranchCreateRequest(BaseModel):
    """특정 메시지 시점에서 새로운 분기 생성 요청."""
    content: str = Field(..., min_length=1)
    model_override: Optional[str] = None
    attachments: List[Any] = Field(default_factory=list)


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
    edited_at: Optional[datetime] = None
    reply_to_id: Optional[uuid.UUID] = None
    branch_id: Optional[uuid.UUID] = None
    branch_point_id: Optional[uuid.UUID] = None
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
    model_config = ConfigDict(populate_by_name=True, serialize_by_name=True)

    id: uuid.UUID
    session_id: uuid.UUID
    workspace_id: Optional[uuid.UUID] = None
    artifact_type: str = Field(..., alias="type")
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


# ─── Prompt Template ────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    title: str = Field(..., max_length=200)
    content: str = Field(..., min_length=1)
    category: str = Field(default="일반", max_length=50)


class TemplateOut(BaseModel):
    id: uuid.UUID
    title: str
    content: str
    category: str
    usage_count: int
    created_at: datetime
    updated_at: datetime
