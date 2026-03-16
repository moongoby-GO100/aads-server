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
async def send_message(request: Request):
    """
    메시지 전송 — SSE 스트리밍 응답.
    Content-Type: text/event-stream
    JSON({session_id, content, model_override, attachments}) 또는
    multipart/form-data(session_id, content, model, files[]) 모두 지원.
    """
    import base64 as _b64

    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        session_id_str = str(form.get("session_id", ""))
        content = str(form.get("content", ""))
        model_override = form.get("model") or form.get("model_override") or None
        attachments = []
        for f in form.getlist("files"):
            if hasattr(f, "read"):
                data = await f.read()
                mime = f.content_type or "application/octet-stream"
                fname = f.filename or "unknown"
                if mime.startswith("image/"):
                    attachments.append({
                        "type": "image",
                        "base64": _b64.b64encode(data).decode(),
                        "media_type": mime,
                        "name": fname,
                    })
                elif mime.startswith("video/"):
                    attachments.append({
                        "type": "video",
                        "base64": _b64.b64encode(data).decode(),
                        "media_type": mime,
                        "name": fname,
                    })
                elif mime == "application/pdf":
                    attachments.append({
                        "type": "pdf",
                        "base64": _b64.b64encode(data).decode(),
                        "name": fname,
                        "media_type": mime,
                    })
                else:
                    try:
                        text_content = data.decode("utf-8", errors="replace")
                        attachments.append({"type": "text", "name": fname, "content": text_content})
                    except Exception:
                        attachments.append({"type": "file", "name": fname})
    else:
        body = await request.json()
        from app.models.chat import MessageSendRequest
        req = MessageSendRequest(**body)
        session_id_str = str(req.session_id)
        content = req.content
        model_override = req.model_override
        attachments = req.attachments

    # ★ ContextVar를 HTTP 핸들러에서 조기 설정
    # with_heartbeat의 ensure_future()가 새 Task를 생성하여 generator 내부의
    # ContextVar.set()이 격리되는 문제 방지 — HTTP task context에서 설정하면
    # 모든 자식 Task가 올바른 session_id를 상속받음
    from app.services.tool_executor import current_chat_session_id
    current_chat_session_id.set(session_id_str)

    stream = svc.with_heartbeat(
        svc.send_message_stream(
            session_id=session_id_str,
            content=content,
            attachments=attachments,
            model_override=model_override,
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
    """세션의 AI 응답 생성 상태 조회 (세션 이동 후 돌아왔을 때 '생성 중' 표시용).

    메모리에 상태가 없을 때 DB에서 streaming_placeholder 존재 여부도 확인
    (서버 재시작으로 메모리 유실된 경우 대비).
    """
    status = svc.get_streaming_status(str(session_id))
    if status and (status.get("is_streaming") or status.get("just_completed")):
        return status
    # 메모리에 없으면 DB에서 placeholder 확인
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            cnt = await conn.fetchval(
                "SELECT count(*) FROM chat_messages WHERE session_id = $1 AND intent = 'streaming_placeholder'",
                session_id,
            )
            if cnt and cnt > 0:
                return {"is_streaming": True, "just_completed": False, "content_length": 0, "tool_count": 0, "last_tool": ""}
    except Exception:
        pass
    return status or {"is_streaming": False}


@router.post("/chat/sessions/{session_id}/stop", tags=["chat-session"])
async def stop_session_streaming(session_id: UUID):
    """세션의 진행 중인 AI 응답 생성을 강제 중단.

    현재까지 생성된 내용과 도구 호출 수를 반환.
    프론트엔드 '중단' 버튼에서 호출하여 백엔드 프로세스까지 완전히 중단.
    """
    result = await svc.stop_session_streaming(str(session_id))
    return result


# ════════════════════════════════════════════════════════════════════════════════
# Interrupt (스트리밍 중 CEO 추가 지시)
# ════════════════════════════════════════════════════════════════════════════════

class InterruptRequest(BaseModel):
    content: str = Field(..., description="스트리밍 중 CEO가 추가로 보내는 지시")


@router.post("/chat/sessions/{session_id}/interrupt", tags=["chat-session"])
async def interrupt_session(session_id: UUID, req: InterruptRequest):
    """스트리밍(AI 응답 생성) 중 CEO 추가 지시를 큐에 삽입.

    is_streaming() 상태일 때만 interrupt_queue에 push.
    아닐 때는 일반 메시지 전송 안내 반환.
    도구 루프 완료 시점에 model_selector.py가 has_interrupt() 체크 후 반영.
    """
    from app.core.interrupt_queue import push_interrupt, is_streaming
    sid = str(session_id)
    if is_streaming(sid):
        push_interrupt(sid, req.content)
        logger.info("interrupt_queued", session_id=sid, content=req.content[:100])
        return {"queued": True, "message": "추가 지시가 다음 도구 완료 시점에 반영됩니다."}
    else:
        return {"queued": False, "message": "현재 AI가 응답 생성 중이 아닙니다. 일반 메시지로 전송하세요."}


@router.post("/chat/sessions/{session_id}/resume", tags=["chat-session"])
async def resume_interrupted(session_id: UUID):
    """서버 재시작으로 중단된 응답을 수동으로 이어서 생성 요청.

    streaming_placeholder가 남아있는 세션에서만 동작.
    이미 이어서 생성 중이면 중복 실행 방지.
    """
    from app.services.chat_service import _resume_single_stream, get_streaming_status
    import re

    sid = str(session_id)

    # 이미 스트리밍 중이면 거부
    status = get_streaming_status(sid)
    if status and status.get("is_streaming"):
        return {"resumed": False, "message": "이미 응답 생성 중입니다."}

    # placeholder 확인
    from app.core.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT m.id AS placeholder_id, m.content AS partial_content,
                   (SELECT content FROM chat_messages
                    WHERE session_id = m.session_id AND role = 'user'
                    ORDER BY created_at DESC LIMIT 1) AS last_user_msg,
                   (SELECT name FROM chat_workspaces w
                    JOIN chat_sessions s ON s.workspace_id = w.id
                    WHERE s.id = m.session_id) AS workspace_name
            FROM chat_messages m
            WHERE m.session_id = $1 AND m.intent = 'streaming_placeholder'
            ORDER BY m.created_at DESC LIMIT 1
        """, session_id)

    if not row:
        return {"resumed": False, "message": "중단된 응답이 없습니다."}

    partial = row["partial_content"] or ""
    clean_partial = re.sub(r'\n\n⏳ _.*?_$', '', partial, flags=re.DOTALL).strip()

    import asyncio
    asyncio.create_task(
        _resume_single_stream(
            sid, row["placeholder_id"], clean_partial,
            row["last_user_msg"] or "", row["workspace_name"] or "CEO",
        )
    )
    return {"resumed": True, "message": "이어서 생성을 시작합니다. 잠시 후 채팅창을 확인하세요."}


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
