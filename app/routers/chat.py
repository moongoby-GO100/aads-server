"""
AADS-170: CEO Chat-First 시스템 — 채팅 라우터
/api/v1/chat/ 하위 엔드포인트.
기존 /api/v1/chat (app/api/chat.py) 와 충돌 없음 — prefix 다름.
"""
from __future__ import annotations

import structlog
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse, Response

from app.models.chat import (
    ApproveDiffOut,
    ApproveDiffRequest,
    ArtifactExportRequest,
    ArtifactOut,
    ArtifactUpdate,
    DriveFileOut,
    MessageOut,
    MessageSendRequest,
    ResearchOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from app.services import chat_service as svc

router = APIRouter()
logger = structlog.get_logger(__name__)

_NOT_FOUND = lambda name: HTTPException(status_code=404, detail=f"{name} not found")


# ════════════════════════════════════════════════════════════════════════════════
# Workspace
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/workspaces", response_model=List[WorkspaceOut], tags=["chat-workspace"])
async def get_workspaces():
    """전체 워크스페이스 목록."""
    return await svc.list_workspaces()


@router.post("/chat/workspaces", response_model=WorkspaceOut, status_code=201, tags=["chat-workspace"])
async def create_workspace(req: WorkspaceCreate):
    """워크스페이스 생성."""
    return await svc.create_workspace(req.model_dump())


@router.put("/chat/workspaces/{workspace_id}", response_model=WorkspaceOut, tags=["chat-workspace"])
async def update_workspace(workspace_id: UUID, req: WorkspaceUpdate):
    """워크스페이스 수정."""
    result = await svc.update_workspace(str(workspace_id), req.model_dump(exclude_none=True))
    if not result:
        raise _NOT_FOUND("workspace")
    return result


@router.delete("/chat/workspaces/{workspace_id}", status_code=204, tags=["chat-workspace"])
async def delete_workspace(workspace_id: UUID):
    """워크스페이스 삭제."""
    ok = await svc.delete_workspace(str(workspace_id))
    if not ok:
        raise _NOT_FOUND("workspace")


# ════════════════════════════════════════════════════════════════════════════════
# Session
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/sessions", response_model=List[SessionOut], tags=["chat-session"])
async def get_sessions(workspace_id: UUID = Query(...)):
    """워크스페이스 내 세션 목록."""
    return await svc.list_sessions(str(workspace_id))


@router.post("/chat/sessions", response_model=SessionOut, status_code=201, tags=["chat-session"])
async def create_session(req: SessionCreate):
    """세션 생성."""
    return await svc.create_session(req.model_dump())


@router.put("/chat/sessions/{session_id}", response_model=SessionOut, tags=["chat-session"])
async def update_session(session_id: UUID, req: SessionUpdate):
    """세션 수정 (title, pinned)."""
    result = await svc.update_session(str(session_id), req.model_dump(exclude_none=True))
    if not result:
        raise _NOT_FOUND("session")
    return result


@router.delete("/chat/sessions/{session_id}", status_code=204, tags=["chat-session"])
async def delete_session(session_id: UUID):
    """세션 삭제."""
    ok = await svc.delete_session(str(session_id))
    if not ok:
        raise _NOT_FOUND("session")


# ════════════════════════════════════════════════════════════════════════════════
# Message
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/messages", response_model=List[MessageOut], tags=["chat-message"])
async def get_messages(
    session_id: UUID = Query(...),
    limit: int = Query(200, le=1000),
    offset: int = Query(0, ge=0),
):
    """메시지 목록."""
    return await svc.list_messages(str(session_id), limit=limit, offset=offset)


@router.post("/chat/messages/send", tags=["chat-message"])
async def send_message(req: MessageSendRequest):
    """
    메시지 전송 — SSE 스트리밍 응답.
    Content-Type: text/event-stream
    """
    session_id_str = str(req.session_id)
    stream = svc.with_heartbeat(
        svc.send_message_stream(
            session_id=session_id_str,
            content=req.content,
            attachments=req.attachments,
            model_override=req.model_override,
        ),
    )
    # 클라이언트 연결 종료 시 백그라운드에서 LLM 생성 완료 → DB 저장 보장
    bg_stream = svc.with_background_completion(stream, session_id=session_id_str)
    return StreamingResponse(
        bg_stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.put("/chat/messages/{message_id}/bookmark", response_model=MessageOut, tags=["chat-message"])
async def toggle_bookmark(message_id: UUID):
    """북마크 토글."""
    result = await svc.toggle_bookmark(str(message_id))
    if not result:
        raise _NOT_FOUND("message")
    return result


@router.get("/chat/messages/search", tags=["chat-message"])
async def search_messages(
    q: str = Query(..., min_length=1),
    workspace_id: Optional[UUID] = Query(None),
    limit: int = Query(20, le=100),
):
    """FTS 전문 검색."""
    results = await svc.search_messages(
        query=q,
        workspace_id=str(workspace_id) if workspace_id else None,
        limit=limit,
    )
    return {"messages": results, "total": len(results)}


# ─── AADS-188D: Diff 승인 API ────────────────────────────────────────────────

_diff_approval_store: dict = {}  # (session_id, tool_use_id) -> action


@router.post("/chat/approve-diff", response_model=ApproveDiffOut, tags=["chat-message"])
async def approve_diff(req: ApproveDiffRequest):
    """
    코드 수정 diff 승인/거부. Monaco DiffEditor UI에서 Accept/Reject 시 호출.
    저장된 결정은 Agent SDK resume 시 참조 가능.
    """
    action = (req.action or "").strip().lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'")
    key = (str(req.session_id), req.tool_use_id)
    _diff_approval_store[key] = action
    logger.info("approve_diff", session_id=str(req.session_id), tool_use_id=req.tool_use_id, action=action)
    return ApproveDiffOut(success=True, action=action, message=f"Diff {action} recorded.")


def get_diff_decision(session_id: str, tool_use_id: str) -> Optional[str]:
    """Agent SDK 등에서 승인 여부 조회 (AADS-188D)."""
    return _diff_approval_store.get((session_id, tool_use_id))


# ════════════════════════════════════════════════════════════════════════════════
# Artifact
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/artifacts", response_model=List[ArtifactOut], tags=["chat-artifact"])
async def get_artifacts(session_id: UUID = Query(...)):
    """세션 내 아티팩트 목록."""
    return await svc.list_artifacts(str(session_id))


@router.get("/chat/artifacts/{artifact_id}", response_model=ArtifactOut, tags=["chat-artifact"])
async def get_artifact(artifact_id: UUID):
    """아티팩트 상세."""
    result = await svc.get_artifact(str(artifact_id))
    if not result:
        raise _NOT_FOUND("artifact")
    return result


@router.put("/chat/artifacts/{artifact_id}", response_model=ArtifactOut, tags=["chat-artifact"])
async def update_artifact(artifact_id: UUID, req: ArtifactUpdate):
    """아티팩트 수정."""
    result = await svc.update_artifact(str(artifact_id), req.model_dump(exclude_none=True))
    if not result:
        raise _NOT_FOUND("artifact")
    return result


@router.post("/chat/artifacts/{artifact_id}/export", tags=["chat-artifact"])
async def export_artifact(artifact_id: UUID, req: ArtifactExportRequest):
    """아티팩트 내보내기 (pdf/md/html)."""
    result = await svc.export_artifact(str(artifact_id), req.format)
    if not result:
        raise _NOT_FOUND("artifact")
    return Response(
        content=result["content"],
        media_type=result["mime"],
        headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"'},
    )


# ════════════════════════════════════════════════════════════════════════════════
# Drive
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/drive", response_model=List[DriveFileOut], tags=["chat-drive"])
async def list_drive(workspace_id: UUID = Query(...)):
    """파일 목록."""
    return await svc.list_drive_files(str(workspace_id))


@router.post("/chat/drive/upload", response_model=DriveFileOut, status_code=201, tags=["chat-drive"])
async def upload_file(
    workspace_id: UUID = Query(...),
    file: UploadFile = File(...),
):
    """파일 업로드 (multipart)."""
    file_bytes = await file.read()
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    result = await svc.save_drive_file(
        workspace_id=str(workspace_id),
        filename=file.filename or "unknown",
        file_bytes=file_bytes,
        file_type=ext or None,
    )
    return result


@router.delete("/chat/drive/{file_id}", status_code=204, tags=["chat-drive"])
async def delete_drive_file(file_id: UUID):
    """파일 삭제."""
    ok = await svc.delete_drive_file(str(file_id))
    if not ok:
        raise _NOT_FOUND("file")


@router.get("/chat/drive/{file_id}/download", tags=["chat-drive"])
async def download_file(file_id: UUID):
    """파일 다운로드."""
    from pathlib import Path
    meta = await svc.get_drive_file(str(file_id))
    if not meta:
        raise _NOT_FOUND("file")
    path = Path(meta["file_path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="file deleted from disk")
    content = path.read_bytes()
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{meta["filename"]}"'},
    )


# ════════════════════════════════════════════════════════════════════════════════
# Research Archive
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/research", response_model=Optional[ResearchOut], tags=["chat-research"])
async def get_research_cache(topic: str = Query(...)):
    """7일 캐시 조회."""
    return await svc.get_research_cache(topic)


@router.get("/chat/research/history", response_model=List[ResearchOut], tags=["chat-research"])
async def get_research_history(limit: int = Query(50, le=200)):
    """전체 조사 이력."""
    return await svc.list_research_history(limit=limit)
