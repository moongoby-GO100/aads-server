"""
AADS-170: CEO Chat-First 시스템 — 채팅 라우터
/api/v1/chat/ 하위 엔드포인트.
기존 /api/v1/chat (app/api/chat.py) 와 충돌 없음 — prefix 다름.
"""
from __future__ import annotations

import structlog
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field

from app.models.chat import (
    ApproveDiffOut,
    ApproveDiffRequest,
    ArtifactExportRequest,
    ArtifactOut,
    ArtifactUpdate,
    DriveFileOut,
    MessageOut,
    MessageSendRequest,
    MessageUpdateRequest,
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
    sort: str = Query("asc", regex="^(asc|desc)$"),
):
    """메시지 목록."""
    return await svc.list_messages(str(session_id), limit=limit, offset=offset, sort=sort)


@router.post("/chat/messages/send", tags=["chat-message"])
async def send_message(req: MessageSendRequest):
    """
    메시지 전송 — SSE 스트리밍 응답.
    Content-Type: text/event-stream
    """
    session_id_str = str(req.session_id)

    # ★ ContextVar를 HTTP 핸들러에서 조기 설정
    # with_heartbeat의 ensure_future()가 새 Task를 생성하여 generator 내부의
    # ContextVar.set()이 격리되는 문제 방지 — HTTP task context에서 설정하면
    # 모든 자식 Task가 올바른 session_id를 상속받음
    from app.services.tool_executor import current_chat_session_id
    current_chat_session_id.set(session_id_str)

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


@router.get("/chat/sessions/{session_id}/streaming-status", tags=["chat-session"])
async def get_streaming_status(session_id: UUID):
    """세션의 AI 응답 생성 상태 조회 (세션 이동 후 돌아왔을 때 '생성 중' 표시용)."""
    status = svc.get_streaming_status(str(session_id))
    if status:
        return status
    return {"is_streaming": False}


@router.put("/chat/messages/{message_id}/bookmark", response_model=MessageOut, tags=["chat-message"])
async def toggle_bookmark(message_id: UUID):
    """북마크 토글."""
    result = await svc.toggle_bookmark(str(message_id))
    if not result:
        raise _NOT_FOUND("message")
    return result


@router.put("/chat/messages/{message_id}", response_model=MessageOut, tags=["chat-message"])
async def update_message(message_id: UUID, req: MessageUpdateRequest):
    """사용자 메시지 내용 수정 (방식A: 수정 후 재전송용)."""
    result = await svc.update_message(str(message_id), req.content)
    if not result:
        raise _NOT_FOUND("message")
    return result


@router.delete("/chat/messages/{message_id}", tags=["chat-message"])
async def delete_message(message_id: UUID):
    """메시지 삭제 + 해당 AI 응답도 함께 삭제 (방식A: 수정재전송 시 기존 응답 제거)."""
    deleted = await svc.delete_message_and_response(str(message_id))
    if not deleted:
        raise _NOT_FOUND("message")
    return {"status": "deleted", "deleted_count": deleted}


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


# ════════════════════════════════════════════════════════════════════════════════
# AADS-190: Frontend Error Reporting
# ════════════════════════════════════════════════════════════════════════════════

class ErrorReportRequest(BaseModel):
    error_type: str = Field(..., description="SSE_DISCONNECT|API_ERROR|STREAM_TIMEOUT|SESSION_SWITCH|UNHANDLED")
    message: str = Field(..., max_length=2000)
    session_id: Optional[str] = None
    url: Optional[str] = None
    stack: Optional[str] = Field(None, max_length=5000)
    context: Optional[dict] = None

class ErrorReportOut(BaseModel):
    ok: bool = True
    error_id: str


# ════════════════════════════════════════════════════════════════════════════════
# Memory Context Viewer (메모리 & 맥락 뷰어)
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/sessions/{session_id}/memory-context", tags=["chat-memory"])
async def get_memory_context(session_id: UUID):
    """세션의 주입 메모리 + 맥락 상태 + 이전 세션 요약 조회."""
    result = await svc.get_memory_context_info(str(session_id))
    if not result or "error" in result:
        raise _NOT_FOUND("session or memory context")
    return result


@router.post("/chat/errors/report", response_model=ErrorReportOut, tags=["chat-errors"])
async def report_frontend_error(req: ErrorReportRequest, request: Request):
    """프론트엔드 에러를 백엔드에 기록 — AI가 다음 턴에서 인지 가능."""
    import uuid
    from datetime import datetime

    error_id = str(uuid.uuid4())[:12]

    # 로그에 구조화된 에러 기록
    logger.warning(
        "frontend_error_report",
        error_id=error_id,
        error_type=req.error_type,
        message=req.message[:500],
        session_id=req.session_id,
        url=req.url,
        client_ip=request.client.host if request.client else None,
    )

    # ai_observations에 저장 → 메모리 주입으로 AI가 인지
    try:
        from app.core.memory_recall import save_observation
        await save_observation(
            category="recurring_issue",
            key=f"frontend_{req.error_type.lower()}",
            content=f"[{datetime.now().strftime('%m/%d %H:%M')}] {req.message[:300]}",
            source="error_reporter",
            confidence=0.4,
        )
    except Exception as e:
        logger.debug(f"error_report_save_failed: {e}")

    return ErrorReportOut(ok=True, error_id=error_id)
