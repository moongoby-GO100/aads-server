"""
AADS-170: CEO Chat-First 시스템 — 채팅 라우터
/api/v1/chat/ 하위 엔드포인트.
기존 /api/v1/chat (app/api/chat.py) 와 충돌 없음 — prefix 다름.
"""
from __future__ import annotations

import json
import re
import time
import asyncio
import structlog
from typing import Any, List, Optional
from uuid import UUID
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field

from app.core.interrupt_queue import is_streaming, push_interrupt, set_streaming
from app.auth import TenantRole, require_tenant_role
from app.models.chat import (
    ApproveDiffOut,
    ApproveDiffRequest,
    ArtifactExportRequest,
    ArtifactOut,
    ArtifactUpdate,
    BranchCreateRequest,
    ChatTodoBulkActionOut,
    ChatTodoBulkActionRequest,
    ChatTodoCreateRequest,
    DriveFileOut,
    ExecutionOut,
    ChatTodoItemOut,
    ChatTodoUpdateRequest,
    MessageOut,
    MessageUpdateRequest,
    ResearchOut,
    SessionCreate,
    SessionOut,
    StreamingStatusOut,
    SessionUpdate,
    TemplateCreate,
    TemplateOut,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from app.services import chat_service as svc

router = APIRouter()
logger = structlog.get_logger(__name__)
_CHAT_MESSAGES_HAS_EDITED_AT: Optional[bool] = None
TenantContext = dict[str, Any]
require_tenant_viewer = require_tenant_role(TenantRole.VIEWER)
require_tenant_member = require_tenant_role(TenantRole.MEMBER)
require_tenant_admin = require_tenant_role(TenantRole.ADMIN)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _NOT_FOUND(name: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{name} not found")


_CODEX_RECONNECT_NOTICE_RE = re.compile(
    r"^\s*⚠️\s*_GPT-[^_\n]+\(Codex CLI\)\s+연결이\s+일시\s+중단되어\s+"
    r"\d+초\s+후\s+동일\s+모델로\s+다시\s+이어갑니다\s+\(\d+/\d+\)\._\s*",
    re.MULTILINE,
)


def _strip_codex_reconnect_notice(content: str) -> str:
    """Codex transport notices are UI/system noise, not user intent."""
    return _CODEX_RECONNECT_NOTICE_RE.sub("", content or "").strip()


async def _chat_messages_has_edited_at(conn) -> bool:
    global _CHAT_MESSAGES_HAS_EDITED_AT
    if _CHAT_MESSAGES_HAS_EDITED_AT is None:
        _CHAT_MESSAGES_HAS_EDITED_AT = bool(await conn.fetchval(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'chat_messages'
              AND column_name = 'edited_at'
            LIMIT 1
            """
        ))
    return _CHAT_MESSAGES_HAS_EDITED_AT


def _encode_revision(count: Optional[int], changed_at) -> str:
    count_value = int(count or 0)
    if not changed_at:
        return f"{count_value}:0"
    return f"{count_value}:{int(changed_at.timestamp() * 1_000_000)}"


async def _get_streaming_status_revisions(session_id: UUID, conn) -> dict:
    has_edited_at = await _chat_messages_has_edited_at(conn)
    message_changed_expr = (
        "GREATEST(m.created_at, COALESCE(m.edited_at, m.created_at))"
        if has_edited_at else
        "m.created_at"
    )
    row = await conn.fetchrow(
        f"""
        SELECT
            msg.message_count,
            msg.message_changed_at,
            ph.placeholder_count,
            ph.placeholder_changed_at,
            art.artifact_count,
            art.artifact_changed_at,
            last_msg.last_message_id
        FROM (
            SELECT
                COUNT(*)::bigint AS message_count,
                MAX({message_changed_expr}) AS message_changed_at
            FROM chat_messages m
            WHERE m.session_id = $1
              AND m.intent IS DISTINCT FROM 'streaming_placeholder'
        ) AS msg
        CROSS JOIN (
            SELECT
                COUNT(*)::bigint AS placeholder_count,
                MAX({message_changed_expr}) AS placeholder_changed_at
            FROM chat_messages m
            WHERE m.session_id = $1
              AND m.intent = 'streaming_placeholder'
        ) AS ph
        CROSS JOIN (
            SELECT
                COUNT(*)::bigint AS artifact_count,
                MAX(a.updated_at) AS artifact_changed_at
            FROM chat_artifacts a
            WHERE a.session_id = $1
        ) AS art
        LEFT JOIN LATERAL (
            SELECT m.id::text AS last_message_id
            FROM chat_messages m
            WHERE m.session_id = $1
              AND m.intent IS DISTINCT FROM 'streaming_placeholder'
            ORDER BY m.created_at DESC
            LIMIT 1
        ) AS last_msg ON TRUE
        """,
        session_id,
    )
    return {
        "last_message_id": row["last_message_id"] if row else None,
        "message_revision": _encode_revision(
            row["message_count"] if row else 0,
            row["message_changed_at"] if row else None,
        ),
        "placeholder_revision": _encode_revision(
            row["placeholder_count"] if row else 0,
            row["placeholder_changed_at"] if row else None,
        ),
        "artifact_revision": _encode_revision(
            row["artifact_count"] if row else 0,
            row["artifact_changed_at"] if row else None,
        ),
    }


async def _finalize_streaming_status(session_id: UUID, result: Optional[dict], conn=None) -> dict:
    payload = dict(result or {"is_streaming": False})
    try:
        if conn is None:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as finalize_conn:
                payload.update(await _get_streaming_status_revisions(session_id, finalize_conn))
        else:
            payload.update(await _get_streaming_status_revisions(session_id, conn))
    except Exception as e:
        logger.debug("streaming-status revision 조회 실패", error=str(e), session_id=str(session_id))
        payload.setdefault("last_message_id", None)
        payload.setdefault("message_revision", None)
        payload.setdefault("placeholder_revision", None)
        payload.setdefault("artifact_revision", None)
    return payload


async def _ensure_running_placeholder_anchor(
    conn,
    session_id: UUID,
    execution_id: str,
    *,
    partial_content: str = "",
    tools_called: Any = None,
    last_event_id: Optional[str] = None,
) -> Optional[dict]:
    """Repair the visible assistant bubble for a live execution."""
    execution_uuid = UUID(execution_id)
    existing = await conn.fetchrow(
        """
        SELECT id, content, tools_called
        FROM chat_messages
        WHERE execution_id = $1
          AND intent = 'streaming_placeholder'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        execution_uuid,
    )
    if existing:
        return existing

    clean_partial = svc._strip_streaming_progress_markers(partial_content or "").strip()
    if clean_partial:
        display_content = clean_partial + "\n\n⏳ _생성 중... (표시 버블 복구됨)_"
    else:
        display_content = "⏳ _AI가 응답을 생성 중입니다... (표시 버블 복구됨)_"
    tools_json = json.dumps(svc.normalize_tool_events(tools_called))
    restored = await conn.fetchrow(
        """
        INSERT INTO chat_messages (
            session_id, execution_id, role, content,
            intent, model_used, tools_called,
            created_at, edited_at
        )
        SELECT $1, $2, 'assistant', $3,
               'streaming_placeholder', 'streaming', $4::jsonb,
               NOW(), NOW()
        WHERE EXISTS (
            SELECT 1
            FROM chat_turn_executions
            WHERE id = $2
              AND status IN ('running', 'retrying')
              AND completed_at IS NULL
        )
        ON CONFLICT (execution_id)
          WHERE intent = 'streaming_placeholder'
            AND execution_id IS NOT NULL
        DO UPDATE
          SET content = EXCLUDED.content,
              tools_called = EXCLUDED.tools_called,
              edited_at = NOW()
        RETURNING id, content, tools_called, (xmax = 0) AS is_new
        """,
        session_id,
        execution_uuid,
        display_content,
        tools_json,
    )
    if not restored:
        return None
    await conn.execute(
        """
        WITH upd_exec AS (
            UPDATE chat_turn_executions te
            SET assistant_message_id = CASE
                    WHEN te.assistant_message_id IS NULL
                      OR NOT EXISTS (
                          SELECT 1
                          FROM chat_messages m
                          WHERE m.id = te.assistant_message_id
                      )
                    THEN $2
                    ELSE te.assistant_message_id
                END,
                last_event_id = COALESCE($3, te.last_event_id),
                updated_at = NOW()
            WHERE te.id = $1
              AND te.status IN ('running', 'retrying')
              AND te.completed_at IS NULL
            RETURNING te.id
        )
        UPDATE chat_sessions
        SET current_execution_id = $1,
            message_count = CASE WHEN $4 THEN message_count + 1 ELSE message_count END,
            updated_at = NOW()
        WHERE id = $5
          AND EXISTS (SELECT 1 FROM upd_exec)
        """,
        execution_uuid,
        restored["id"],
        last_event_id,
        bool(restored["is_new"]),
        session_id,
    )
    logger.warning(
        "streaming_status_repaired_missing_placeholder session=%s execution=%s placeholder=%s is_new=%s",
        str(session_id)[:8],
        execution_id[:8],
        str(restored["id"])[:8],
        bool(restored["is_new"]),
    )
    return restored


def _extract_tool_progress(tools_called: Any) -> tuple[int, str]:
    """Return tool progress from persisted tool events."""
    try:
        tool_uses = [
            t for t in svc.normalize_tool_events(tools_called)
            if t.get("type") == "tool_use"
        ]
        last_tool = tool_uses[-1].get("tool_name", "") if tool_uses else ""
        return len(tool_uses), last_tool
    except Exception:
        return 0, ""


def _has_live_streaming_runtime(session_id: UUID | str, cached_status: Optional[dict] = None) -> bool:
    """Check whether a session still has an in-process producer on this server."""
    sid = str(session_id)
    if cached_status and cached_status.get("is_streaming"):
        return True
    if is_streaming(sid):
        return True
    _state = getattr(svc, "_streaming_state", {}).get(sid)
    if _state and not _state.get("completed", False):
        return True
    _task = getattr(svc, "_active_bg_tasks", {}).get(sid)
    return bool(_task is not None and not _task.done())


async def _schedule_recovery_auto_resume(
    conn,
    session_id: UUID,
    execution_id: UUID,
    assistant_message_id: Optional[UUID],
    partial_content: str,
) -> bool:
    """Turn a recovered stale chat execution back into a retrying stream.

    Recovery endpoints previously surfaced partial content and terminalized the
    execution. For chat UX, a recoverable stale stream should keep answering
    automatically when we still have the original user message and retry budget.
    """
    try:
        row = await conn.fetchrow(
            """
            SELECT te.retry_count,
                   te.requested_model,
                   COALESCE(um.content, '') AS last_user_msg,
                   w.name AS workspace_name
            FROM chat_turn_executions te
            JOIN chat_sessions s ON s.id = te.session_id
            JOIN chat_workspaces w ON w.id = s.workspace_id
            LEFT JOIN chat_messages um ON um.id = te.user_message_id
            WHERE te.id = $1
              AND te.session_id = $2
            """,
            execution_id,
            session_id,
        )
        if not row:
            return False
        if (row["retry_count"] or 0) >= 5:
            logger.warning(
                "recovery_auto_resume_skip_hard_cap session=%s execution=%s retry_count=%s",
                str(session_id)[:8],
                str(execution_id)[:8],
                row["retry_count"],
            )
            return False
        if not (row["last_user_msg"] or "").strip():
            logger.warning(
                "recovery_auto_resume_skip_no_user session=%s execution=%s",
                str(session_id)[:8],
                str(execution_id)[:8],
            )
            return False

        clean_partial = svc._strip_streaming_progress_markers(partial_content or "")
        clean_partial = re.sub(
            r"\n\n_\(응답이 중단되어 여기까지 보존되었습니다\.\)_\s*$",
            "",
            clean_partial,
            flags=re.DOTALL,
        ).strip()
        placeholder_id = assistant_message_id
        if placeholder_id:
            await conn.execute(
                """
                UPDATE chat_messages
                SET content = $2,
                    intent = 'streaming_placeholder',
                    model_used = 'streaming',
                    edited_at = NOW()
                WHERE id = $1
                """,
                placeholder_id,
                (
                    clean_partial
                    + "\n\n⏳ _생성 중... (자동 이어쓰기 재시도 중)_"
                    if clean_partial
                    else "⏳ _AI가 응답을 다시 생성 중입니다..._"
                ),
            )
        else:
            placeholder_id = await conn.fetchval(
                """
                INSERT INTO chat_messages (
                    session_id, execution_id, role, content,
                    intent, model_used, tools_called
                )
                VALUES ($1, $2, 'assistant', $3, 'streaming_placeholder', 'streaming', '[]'::jsonb)
                RETURNING id
                """,
                session_id,
                execution_id,
                (
                    clean_partial
                    + "\n\n⏳ _생성 중... (자동 이어쓰기 재시도 중)_"
                    if clean_partial
                    else "⏳ _AI가 응답을 다시 생성 중입니다..._"
                ),
            )

        claimed = await conn.fetchval(
            """
            UPDATE chat_turn_executions
            SET status = 'retrying',
                retry_count = retry_count + 1,
                assistant_message_id = $2,
                completed_at = NULL,
                error_message = 'recovery_auto_retry_scheduled',
                updated_at = NOW()
            WHERE id = $1
              AND session_id = $3
              AND status = 'interrupted'
              AND retry_count < 5
            RETURNING id
            """,
            execution_id,
            placeholder_id,
            session_id,
        )
        if not claimed:
            return False

        await conn.execute(
            """
            UPDATE chat_sessions
            SET current_execution_id = $2,
                updated_at = NOW()
            WHERE id = $1
            """,
            session_id,
            execution_id,
        )

        task = asyncio.create_task(
            svc._resume_single_stream(
                str(session_id),
                placeholder_id,
                clean_partial,
                row["last_user_msg"] or "",
                row["workspace_name"] or "CEO",
                execution_id=str(execution_id),
                requested_model=row["requested_model"],
            )
        )

        def _on_recovery_resume_done(_task, _sid=str(session_id), _eid=str(execution_id)):
            if _task.cancelled():
                logger.warning(
                    "recovery_auto_resume_cancelled session=%s execution=%s",
                    _sid[:8],
                    _eid[:8],
                )
                return
            exc = _task.exception()
            if exc:
                logger.error(
                    "recovery_auto_resume_error session=%s execution=%s error=%s",
                    _sid[:8],
                    _eid[:8],
                    exc,
                )

        task.add_done_callback(_on_recovery_resume_done)
        logger.info(
            "recovery_auto_resume_scheduled session=%s execution=%s partial_len=%s",
            str(session_id)[:8],
            str(execution_id)[:8],
            len(clean_partial),
        )
        return True
    except Exception as exc:
        logger.warning(
            "recovery_auto_resume_schedule_failed session=%s execution=%s error=%s",
            str(session_id)[:8],
            str(execution_id)[:8],
            exc,
        )
        return False


async def _settle_stale_execution_for_recovery(
    conn,
    session_id: UUID,
    execution_row: Any,
    *,
    has_live_runtime: bool = False,
) -> Optional[dict]:
    """Terminalize dead running executions so recovery APIs stop hiding final content."""
    if not execution_row or execution_row["status"] not in ("running", "retrying"):
        return None

    _partial = execution_row["partial_content"] or ""
    _tc, _lt = _extract_tool_progress(execution_row["tools_called"])
    _clean_partial = svc._strip_streaming_progress_markers(_partial)
    _first_response_grace = int(getattr(svc, "_FIRST_RESPONSE_TIMEOUT_SEC", 120)) + 30
    _recovery_grace = 10
    _no_db_progress = (
        not svc._has_meaningful_partial_content(_clean_partial)
        and not execution_row["last_event_id"]
        and _tc == 0
    )
    _stale_empty_execution = (
        _no_db_progress
        and int(execution_row["updated_age_seconds"] or 0) >= _first_response_grace
    )
    _stale_empty_no_runtime = (
        _no_db_progress
        and not has_live_runtime
        and int(execution_row["updated_age_seconds"] or 0) >= 30
    )
    _stale_progressed_execution = (
        not has_live_runtime
        and int(execution_row["updated_age_seconds"] or 0) >= _recovery_grace
        and (
            svc._has_meaningful_partial_content(_clean_partial)
            or bool(execution_row["last_event_id"])
            or _tc > 0
        )
    )
    _started_age = int(execution_row.get("started_age_seconds") or 0)
    _updated_age = int(execution_row.get("updated_age_seconds") or 0)
    _has_tool_activity = _tc > 0 or bool(execution_row.get("last_event_id"))
    _hard_cutoff_sec = 900 if _has_tool_activity else 300
    _hard_stale_by_started_at = (
        not has_live_runtime
        and _started_age >= _hard_cutoff_sec
        and _updated_age >= 120
    )
    if execution_row["updated_recently"] and not (
        _stale_empty_execution or _stale_progressed_execution or _stale_empty_no_runtime
        or _hard_stale_by_started_at
    ):
        return None

    _execution_uuid = UUID(execution_row["execution_id"])
    _assistant_id = None
    if _clean_partial and svc._has_meaningful_partial_content(_clean_partial):
        _final_partial = (
            _clean_partial
            if "응답이 중단" in _clean_partial
            else _clean_partial + "\n\n_(응답이 중단되어 여기까지 보존되었습니다.)_"
        )
        _assistant_id = await conn.fetchval(
            """
            UPDATE chat_messages
            SET content = $2,
                intent = 'interrupted_partial',
                model_used = 'interrupted',
                edited_at = NOW()
            WHERE execution_id = $1
              AND intent = 'streaming_placeholder'
            RETURNING id
            """,
            _execution_uuid,
            _final_partial,
        )
    else:
        _deleted_placeholder_id = await conn.fetchval(
            """
            DELETE FROM chat_messages
            WHERE execution_id = $1
              AND intent = 'streaming_placeholder'
            RETURNING id
            """,
            _execution_uuid,
        )
        if _deleted_placeholder_id:
            await conn.execute(
                "UPDATE chat_sessions SET message_count = GREATEST(message_count - 1, 0), updated_at = NOW() WHERE id = $1",
                session_id,
            )

    await conn.execute(
        """
        UPDATE chat_turn_executions
        SET status = 'interrupted',
            assistant_message_id = CASE
                WHEN $2::uuid IS NULL THEN assistant_message_id
                ELSE $2::uuid
            END,
            completed_at = COALESCE(completed_at, NOW()),
            updated_at = NOW(),
            error_message = COALESCE(error_message, 'stale running execution settled by recovery endpoint')
        WHERE id = $1
          AND status IN ('running', 'retrying')
        """,
        _execution_uuid,
        _assistant_id,
    )
    await conn.execute(
        """
        UPDATE chat_sessions
        SET current_execution_id = NULL,
            updated_at = NOW()
        WHERE id = $1
          AND current_execution_id = $2
        """,
        session_id,
        _execution_uuid,
    )
    _auto_retry_scheduled = await _schedule_recovery_auto_resume(
        conn,
        session_id,
        _execution_uuid,
        _assistant_id,
        _clean_partial,
    )
    return {
        "is_streaming": bool(_auto_retry_scheduled),
        "just_completed": bool(_assistant_id and _clean_partial and not _auto_retry_scheduled),
        "auto_retry_scheduled": bool(_auto_retry_scheduled),
        "content_length": len(_clean_partial),
        "tool_count": _tc,
        "last_tool": _lt,
        "execution_id": execution_row["execution_id"],
        "last_event_id": execution_row["last_event_id"],
    }


async def _settle_or_surface_orphan_placeholder(
    conn,
    session_id: UUID,
    placeholder_row: Any,
) -> Optional[dict]:
    """Make a DB-only placeholder visible when no producer can finish it."""
    if not placeholder_row:
        return None

    _partial = placeholder_row["content"] or ""
    _clean_partial = svc._strip_streaming_progress_markers(_partial)
    if svc._has_meaningful_partial_content(_clean_partial):
        _final_partial = (
            _clean_partial
            if "응답이 중단" in _clean_partial
            else _clean_partial + "\n\n_(응답이 중단되어 여기까지 보존되었습니다.)_"
        )
        updated = await conn.fetchrow(
            """
            UPDATE chat_messages
            SET content = $2,
                intent = 'interrupted_partial',
                model_used = 'interrupted',
                edited_at = NOW()
            WHERE id = $1
              AND intent = 'streaming_placeholder'
            RETURNING id::text, content, model_used, intent, execution_id::text AS execution_id,
                      created_at::text AS created_at_text
            """,
            placeholder_row["id"],
            _final_partial,
        )
        if not updated:
            return None
        return {
            "found": True,
            "message": {
                "id": updated["id"],
                "session_id": str(session_id),
                "role": "assistant",
                "content": updated["content"],
                "model_used": updated["model_used"],
                "created_at": updated["created_at_text"],
                "intent": updated["intent"],
                "execution_id": updated["execution_id"],
            },
            "status": {
                "is_streaming": False,
                "just_completed": True,
                "content_length": len(_clean_partial),
                "tool_count": _extract_tool_progress(placeholder_row["tools_called"])[0],
                "last_tool": _extract_tool_progress(placeholder_row["tools_called"])[1],
                "partial_content": updated["content"],
                "execution_id": updated["execution_id"],
                "last_event_id": None,
            },
        }

    deleted = await conn.fetchval(
        """
        DELETE FROM chat_messages
        WHERE id = $1
          AND intent = 'streaming_placeholder'
        RETURNING id
        """,
        placeholder_row["id"],
    )
    if deleted:
        await conn.execute(
            "UPDATE chat_sessions SET message_count = GREATEST(message_count - 1, 0), updated_at = NOW() WHERE id = $1",
            session_id,
        )
    return {
        "found": False,
        "status": {
            "is_streaming": False,
            "just_completed": False,
            "content_length": 0,
            "tool_count": 0,
            "last_tool": "",
            "execution_id": None,
            "last_event_id": None,
        },
    }


# ════════════════════════════════════════════════════════════════════════════════
# Workspace
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/workspaces", response_model=List[WorkspaceOut], tags=["chat-workspace"])
async def get_workspaces(context: TenantContext = Depends(require_tenant_viewer)):
    """전체 워크스페이스 목록."""
    return await svc.list_workspaces(tenant_id=_tenant_id(context))


@router.get("/chat/workspaces/{workspace_id}/roles", tags=["chat-workspace"])
async def get_workspace_roles(
    workspace_id: UUID,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """워크스페이스/프로젝트 기준 DB 역할 목록."""
    roles = await svc.list_workspace_roles(str(workspace_id), tenant_id=_tenant_id(context))
    if not roles:
        raise _NOT_FOUND("workspace")
    return {"roles": roles, "total": len(roles)}


@router.post("/chat/workspaces", response_model=WorkspaceOut, status_code=201, tags=["chat-workspace"])
async def create_workspace(
    req: WorkspaceCreate,
    context: TenantContext = Depends(require_tenant_admin),
):
    """워크스페이스 생성."""
    return await svc.create_workspace(req.model_dump(), tenant_id=_tenant_id(context))


@router.put("/chat/workspaces/{workspace_id}", response_model=WorkspaceOut, tags=["chat-workspace"])
async def update_workspace(
    workspace_id: UUID,
    req: WorkspaceUpdate,
    context: TenantContext = Depends(require_tenant_admin),
):
    """워크스페이스 수정."""
    result = await svc.update_workspace(str(workspace_id), req.model_dump(exclude_none=True), tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("workspace")
    return result


@router.delete("/chat/workspaces/{workspace_id}", status_code=204, tags=["chat-workspace"])
async def delete_workspace(
    workspace_id: UUID,
    context: TenantContext = Depends(require_tenant_admin),
):
    """워크스페이스 삭제."""
    ok = await svc.delete_workspace(str(workspace_id), tenant_id=_tenant_id(context))
    if not ok:
        raise _NOT_FOUND("workspace")


# ════════════════════════════════════════════════════════════════════════════════
# Session
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/sessions", response_model=List[SessionOut], tags=["chat-session"])
async def get_sessions(
    workspace_id: UUID = Query(...),
    tag: Optional[str] = Query(None),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """워크스페이스 내 세션 목록. tag 파라미터로 필터 가능."""
    return await svc.list_sessions(str(workspace_id), tag=tag, tenant_id=_tenant_id(context))


@router.get("/chat/sessions/{session_id}", response_model=SessionOut, tags=["chat-session"])
async def get_session(
    session_id: UUID,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """단일 세션 조회 (해시 기반 세션 복원용)."""
    result = await svc.get_session(str(session_id), tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("session")
    return result


@router.get("/chat/sessions/{session_id}/execution", response_model=ExecutionOut, tags=["chat-session"])
async def get_session_execution(
    session_id: UUID,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """현재 세션의 최신 실행 조회."""
    result = await svc.get_current_execution(str(session_id), tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("execution")
    return result


@router.get("/chat/executions/{execution_id}", response_model=ExecutionOut, tags=["chat-session"])
async def get_execution(
    execution_id: UUID,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """단일 실행 조회."""
    result = await svc.get_execution(str(execution_id), tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("execution")
    return result


@router.post("/chat/sessions", response_model=SessionOut, status_code=201, tags=["chat-session"])
async def create_session(
    req: SessionCreate,
    context: TenantContext = Depends(require_tenant_member),
):
    """세션 생성."""
    try:
        return await svc.create_session(req.model_dump(), tenant_id=_tenant_id(context))
    except ValueError as e:
        if str(e) == "workspace_not_found_for_tenant":
            raise _NOT_FOUND("workspace")
        raise


@router.put("/chat/sessions/{session_id}", response_model=SessionOut, tags=["chat-session"])
async def update_session(
    session_id: UUID,
    req: SessionUpdate,
    context: TenantContext = Depends(require_tenant_member),
):
    """세션 수정 (title, pinned)."""
    result = await svc.update_session(str(session_id), req.model_dump(exclude_none=True), tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("session")
    return result


@router.delete("/chat/sessions/{session_id}", status_code=204, tags=["chat-session"])
async def delete_session(
    session_id: UUID,
    context: TenantContext = Depends(require_tenant_admin),
):
    """세션 삭제."""
    ok = await svc.delete_session(str(session_id), tenant_id=_tenant_id(context))
    if not ok:
        raise _NOT_FOUND("session")


# ════════════════════════════════════════════════════════════════════════════════
# Message
# ════════════════════════════════════════════════════════════════════════════════

class PaginatedMessagesOut(BaseModel):
    """Cursor 기반 페이지네이션 응답."""
    messages: List[MessageOut]
    next_cursor: Optional[str] = None
    has_more: bool = False


def _message_row_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        return len(payload["messages"])
    return 0


def _payload_size_bytes(payload: Any) -> int:
    body = json.dumps(
        jsonable_encoder(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return len(body.encode("utf-8"))


def _set_message_response_headers(response: Response, started_at: float, payload: Any) -> None:
    payload_bytes = _payload_size_bytes(payload)
    response.headers["X-Response-Time"] = f"{(time.perf_counter() - started_at) * 1000:.2f}ms"
    response.headers["X-Payload-Bytes"] = str(payload_bytes)
    response.headers["X-Row-Count"] = str(_message_row_count(payload))


async def _get_messages_payload(
    session_id: UUID,
    *,
    limit: int,
    cursor: Optional[str],
    offset: Optional[int],
    sort: str,
    include_streaming: bool,
    fields: str,
    tenant_id: str,
) -> Any:
    read_only = fields == "minimal"
    # 레거시 offset 모드: offset이 명시적으로 전달되거나, sort=desc(배열 기대)인 경우
    # 프론트엔드 6곳에서 sort=desc + ChatMessage[] 배열을 기대하므로 반드시 배열 반환
    if offset is not None or (sort == "desc" and cursor is None):
        return await svc.list_messages(
            str(session_id),
            limit=limit,
            offset=offset or 0,
            sort=sort,
            include_streaming=include_streaming,
            fields=fields,
            read_only=read_only,
            tenant_id=tenant_id,
        )
    # cursor 모드: PaginatedMessagesOut 반환 (항상 ASC)
    return await svc.list_messages_cursor(
        str(session_id),
        limit=limit,
        cursor=cursor,
        include_streaming=include_streaming,
        fields=fields,
        read_only=read_only,
        tenant_id=tenant_id,
    )


@router.get("/chat/messages", tags=["chat-message"])
async def get_messages(
    response: Response,
    session_id: UUID = Query(...),
    limit: int = Query(50, le=1000),
    cursor: Optional[str] = Query(None, description="created_at ISO 문자열 (이전 메시지 로딩 시)"),
    offset: Optional[int] = Query(None, ge=0),
    sort: str = Query("asc", pattern="^(asc|desc)$"),
    include_streaming: bool = Query(False, description="진행 중 streaming_placeholder 포함 여부"),
    fields: str = Query("full", pattern="^(full|minimal)$"),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """메시지 목록 — cursor 기반 페이지네이션 (offset 레거시 호환 유지)."""
    started_at = time.perf_counter()
    payload = await _get_messages_payload(
        session_id,
        limit=limit,
        cursor=cursor,
        offset=offset,
        sort=sort,
        include_streaming=include_streaming,
        fields=fields,
        tenant_id=_tenant_id(context),
    )
    _set_message_response_headers(response, started_at, payload)
    return payload


@router.get("/chat/{workspace_id}/sessions/{session_id}/messages", tags=["chat-message"])
async def get_workspace_session_messages(
    workspace_id: UUID,
    session_id: UUID,
    response: Response,
    limit: int = Query(50, le=1000),
    cursor: Optional[str] = Query(None, description="created_at ISO 문자열 (이전 메시지 로딩 시)"),
    offset: Optional[int] = Query(None, ge=0),
    sort: str = Query("asc", pattern="^(asc|desc)$"),
    include_streaming: bool = Query(False, description="진행 중 streaming_placeholder 포함 여부"),
    fields: str = Query("full", pattern="^(full|minimal)$"),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """워크스페이스 경로 메시지 목록 — 기존 /chat/messages와 동일한 응답 계약."""
    del workspace_id
    started_at = time.perf_counter()
    payload = await _get_messages_payload(
        session_id,
        limit=limit,
        cursor=cursor,
        offset=offset,
        sort=sort,
        include_streaming=include_streaming,
        fields=fields,
        tenant_id=_tenant_id(context),
    )
    _set_message_response_headers(response, started_at, payload)
    return payload


@router.post("/chat/messages/send", tags=["chat-message"])
async def send_message(
    request: Request,
    context: TenantContext = Depends(require_tenant_member),
):
    """
    메시지 전송 — SSE 스트리밍 응답.
    Content-Type: text/event-stream
    JSON({session_id, content, model_override, attachments}) 또는
    multipart/form-data(session_id, content, model, files[]) 모두 지원.
    """
    import base64 as _b64

    content_type = request.headers.get("content-type", "")

    _MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB

    reply_to_id = None  # multipart에서도 초기화

    if "multipart/form-data" in content_type:
        form = await request.form()
        session_id_str = str(form.get("session_id", ""))
        content = _strip_codex_reconnect_notice(str(form.get("content", "")))
        model_override = form.get("model") or form.get("model_override") or None
        response_mode = str(form.get("response_mode") or "quality")
        reply_to_id = str(form.get("reply_to_id")) if form.get("reply_to_id") else None
        idempotency_key = str(form.get("idempotency_key")) if form.get("idempotency_key") else None
        attachments = []
        for f in form.getlist("files"):
            if hasattr(f, "read"):
                data = await f.read()
                if len(data) > _MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail=f"파일 크기 초과: {len(data)} bytes > {_MAX_UPLOAD_SIZE} bytes (50MB 제한)")
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
                    except Exception as e:
                        logger.debug("첨부파일 텍스트 디코딩 실패", filename=fname, error=str(e))
                        attachments.append({"type": "file", "name": fname})
    else:
        body = await request.json()
        from app.models.chat import MessageSendRequest
        req = MessageSendRequest(**body)
        session_id_str = str(req.session_id)
        content = _strip_codex_reconnect_notice(req.content)
        model_override = req.model_override
        response_mode = req.response_mode
        attachments = req.attachments
        reply_to_id = str(req.reply_to_id) if req.reply_to_id else None
        idempotency_key = req.idempotency_key if hasattr(req, 'idempotency_key') else None

    if not session_id_str or not await svc.get_session(session_id_str, tenant_id=_tenant_id(context)):
        raise _NOT_FOUND("session")

    tenant_id = _tenant_id(context)
    from app.services.tenant_usage_limits import TenantUsageLimitExceeded, check_tenant_usage_limit
    from app.services.tool_executor import current_tenant_id

    current_tenant_id.set(tenant_id)
    try:
        await check_tenant_usage_limit(
            tenant_id,
            operation="chat:send_message",
            projected_calls=1,
        )
    except TenantUsageLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.decision.message) from e

    if is_streaming(session_id_str):
        push_interrupt(
            session_id_str,
            message=content,
            attachments=attachments if isinstance(attachments, list) else None,
        )
        logger.info(
            "send_message_interrupt_queued",
            session_id=session_id_str,
            attachment_count=len(attachments or []),
        )
        return {"status": "interrupt_queued", "session_id": session_id_str}

    # ★ ContextVar를 HTTP 핸들러에서 조기 설정
    # with_background_completion 내부의 producer Task가 올바른 session_id를 상속받도록
    from app.services.tool_executor import current_chat_session_id
    current_chat_session_id.set(session_id_str)
    html_context_state = await svc.get_html_edit_context_state(session_id_str, content)

    # with_background_completion이 독립 heartbeat task(_heartbeat_pump)를 운영하므로
    # with_heartbeat 이중 래핑 불필요 — 도구 30s+ 블로킹 시에도 heartbeat 보장
    raw_stream = svc.send_message_stream(
        session_id=session_id_str,
        content=content,
        attachments=attachments,
        model_override=model_override,
        response_mode=response_mode,
        reply_to_id=reply_to_id,
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
    )
    # 클라이언트 연결 종료 시 백그라운드에서 LLM 생성 완료 → DB 저장 보장
    bg_stream = svc.with_background_completion(raw_stream, session_id=session_id_str)
    return StreamingResponse(
        bg_stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "X-Stream-Session": session_id_str,
            "X-HTML-Context-Used": "true" if html_context_state.get("html_context_used") else "false",
        },
    )


class DiscussionRequest(BaseModel):
    content: str = Field(..., min_length=1, description="토론할 질문")
    context: Optional[str] = Field(None, description="추가 배경 컨텍스트")
    perspectives: List[dict[str, Any]] = Field(default_factory=list, description="선택 관점 설정")


class DiscussionResponse(BaseModel):
    question: str
    message: str
    synthesis: str
    perspectives: List[dict[str, Any]] = Field(default_factory=list)
    cost_usd: float
    duration_ms: int
    debate_id: str


@router.post("/chat/sessions/{session_id}/discussion", response_model=DiscussionResponse, tags=["chat-session"])
async def run_discussion(
    session_id: UUID,
    req: DiscussionRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """세션 기준 다관점 토론 실행."""
    if not await svc.get_session(str(session_id), tenant_id=_tenant_id(context)):
        raise _NOT_FOUND("session")
    return await svc.run_discussion(
        str(session_id),
        req.content,
        context=req.context or "",
        perspectives=req.perspectives or None,
    )


# ── Multi-LLM Discussion Orchestrator endpoints ──

class MultiDiscussionStartRequest(BaseModel):
    topic: str = Field(..., min_length=1, description="토론 주제")
    mode: str = Field("manual", description="manual 또는 auto")
    preset: str = Field("standard", description="프리셋: standard, deep, light")
    custom_participants: Optional[List[dict]] = Field(None, description="커스텀 참가자 목록")
    budget_usd: float = Field(10.0, ge=0.1, le=100.0, description="예산 한도 (USD)")
    synthesizer_model: Optional[str] = Field(None, description="종합 모델")


class MultiDiscussionContinueRequest(BaseModel):
    message: str = Field(..., min_length=1, description="CEO 메시지 (다음/계속/그만 또는 지시)")


class DiscussionDirectiveRequest(BaseModel):
    directive: str = Field(..., min_length=1, description="CEO 실시간 지시")


@router.post("/chat/sessions/{session_id}/discussion/start", tags=["discussion"])
async def start_multi_discussion(session_id: UUID, req: MultiDiscussionStartRequest):
    """멀티-LLM 토론 시작 — SSE 스트리밍."""
    from app.services.discussion_orchestrator import orchestrator, DiscussionMode
    mode = DiscussionMode.AUTO if req.mode == "auto" else DiscussionMode.MANUAL
    gen = orchestrator.start_discussion(
        session_id=str(session_id),
        topic=req.topic,
        mode=mode,
        preset=req.preset,
        custom_participants=req.custom_participants,
        budget_usd=req.budget_usd,
        synthesizer_model=req.synthesizer_model,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/chat/sessions/{session_id}/discussion/continue", tags=["discussion"])
async def continue_multi_discussion(session_id: UUID, req: MultiDiscussionContinueRequest):
    """CEO 개입/계속/종료 — SSE 스트리밍."""
    from app.services.discussion_orchestrator import orchestrator
    gen = orchestrator.continue_discussion(str(session_id), req.message)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/chat/sessions/{session_id}/discussion/status", tags=["discussion"])
async def get_multi_discussion_status(session_id: UUID):
    """활성 토론 상태 조회."""
    from app.services.discussion_orchestrator import orchestrator
    state = orchestrator.get_active_discussion(str(session_id))
    if state is None:
        return {"active": False, "message": "활성 토론이 없습니다."}
    status = orchestrator.get_discussion_status(state.discussion_id)
    return {"active": True, **status}


@router.post("/chat/sessions/{session_id}/discussion/stop", tags=["discussion"])
async def stop_multi_discussion(session_id: UUID):
    """토론 강제 취소."""
    from app.services.discussion_orchestrator import orchestrator
    state = orchestrator.get_active_discussion(str(session_id))
    if state is None:
        raise HTTPException(status_code=404, detail="활성 토론이 없습니다.")
    orchestrator.cancel_discussion(state.discussion_id)
    return {"cancelled": True, "discussion_id": state.discussion_id}


@router.post("/chat/sessions/{session_id}/discussion/directive", tags=["discussion"])
async def inject_discussion_directive(session_id: UUID, req: DiscussionDirectiveRequest):
    """자동 모드 중 CEO 지시 주입."""
    from app.services.discussion_orchestrator import orchestrator
    state = orchestrator.get_active_discussion(str(session_id))
    if state is None:
        raise HTTPException(status_code=404, detail="활성 토론이 없습니다.")
    ok = orchestrator.inject_ceo_directive(state.discussion_id, req.directive)
    return {"injected": ok, "directive": req.directive}


@router.get("/chat/discussion/presets", tags=["discussion"])
async def list_discussion_presets():
    """사용 가능한 토론 프리셋 목록."""
    from app.services.discussion_presets import DISCUSSION_PRESETS
    return {"presets": DISCUSSION_PRESETS}


@router.get("/chat/sessions/{session_id}/streaming-status", response_model=StreamingStatusOut, tags=["chat-session"])
async def get_streaming_status(session_id: UUID):
    """세션의 AI 응답 생성 상태 조회 (세션 이동 후 돌아왔을 때 '생성 중' 표시용).

    메모리에 상태가 없을 때 DB에서 streaming_placeholder 존재 여부도 확인
    (서버 재시작으로 메모리 유실된 경우 대비).
    서버 재시작 후 recovered 메시지가 있으면 just_completed+recovered=True 반환
    (클라이언트가 메시지를 다시 로드하도록 트리거).
    응답에는 revision 필드가 포함되며, 클라이언트는 이전 값과 같으면 /messages 재조회를 생략할 수 있다.
    """
    def _looks_terminal_interrupt(content: str) -> bool:
        content = str(content or "")
        return (
            "최신 지시를 우선 처리" in content
            or "중단 처리되었습니다" in content
            or "응답 생성이 중단" in content
        )

    status = svc.get_streaming_status(str(session_id))
    memory_terminal_status = None
    if status:
        if status.get("is_streaming"):
            if not _looks_terminal_interrupt(status.get("partial_content", "")):
                return await _finalize_streaming_status(session_id, status)
            memory_terminal_status = {
                **status,
                "is_streaming": False,
                "just_completed": True,
            }
        elif status.get("just_completed"):
            memory_terminal_status = status
    _has_live_runtime = _has_live_streaming_runtime(session_id, status)
    # 메모리에 없으면 DB에서 placeholder 확인 (5분 이내만 유효)
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            execution_row = await conn.fetchrow(
                """
                SELECT te.id::text AS execution_id,
                       te.status,
                       te.last_event_id,
                       te.updated_at,
                       EXTRACT(EPOCH FROM (NOW() - te.updated_at))::int AS updated_age_seconds,
                       EXTRACT(EPOCH FROM (NOW() - te.started_at))::int AS started_age_seconds,
                       (te.updated_at > NOW() - interval '5 minutes') AS updated_recently,
                       te.completed_at,
                       am.model_used AS assistant_model_used,
                       pm.id::text AS placeholder_id,
                       CASE
                           WHEN te.status IN ('running', 'retrying') THEN COALESCE(pm.content, am.content)
                           ELSE COALESCE(am.content, pm.content)
                       END AS partial_content,
                       CASE
                           WHEN te.status IN ('running', 'retrying') THEN COALESCE(pm.tools_called, am.tools_called)
                           ELSE COALESCE(am.tools_called, pm.tools_called)
                       END AS tools_called
                FROM chat_sessions s
                LEFT JOIN chat_turn_executions te
                  ON te.id = COALESCE(
                      s.current_execution_id,
                      (
                          SELECT te_latest.id
                          FROM chat_turn_executions te_latest
                          WHERE te_latest.session_id = s.id
                            AND te_latest.status IN ('running', 'retrying')
                          ORDER BY te_latest.updated_at DESC
                          LIMIT 1
                      )
                  )
                LEFT JOIN chat_messages am
                  ON am.id = te.assistant_message_id
                LEFT JOIN LATERAL (
                    SELECT id, content, tools_called
                    FROM chat_messages
                    WHERE execution_id = te.id
                      AND intent = 'streaming_placeholder'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) pm ON TRUE
                WHERE s.id = $1
                """,
                session_id,
            )

            def _extract_tool_progress(tools_called) -> tuple[int, str]:
                """tools_called JSON에서 tool_count/last_tool 산출 (BUG #3).
                asyncpg는 jsonb를 str로 반환할 수 있어 list/str 양쪽 처리."""
                try:
                    tool_uses = [
                        t for t in svc.normalize_tool_events(tools_called)
                        if t.get("type") == "tool_use"
                    ]
                    last_tool = tool_uses[-1].get("tool_name", "") if tool_uses else ""
                    return len(tool_uses), last_tool
                except Exception:
                    return 0, ""

            if execution_row and execution_row["execution_id"]:
                if (
                    execution_row["status"] in ("running", "retrying")
                    and execution_row["assistant_model_used"] in ("interrupted", "stopped")
                ):
                    await conn.execute(
                        """
                        UPDATE chat_turn_executions
                        SET status = 'interrupted',
                            completed_at = COALESCE(completed_at, NOW()),
                            updated_at = NOW(),
                            error_message = COALESCE(error_message, 'assistant message already terminal')
                        WHERE id = $1
                          AND status IN ('running', 'retrying')
                        """,
                        UUID(execution_row["execution_id"]),
                    )
                    await conn.execute(
                        """
                        UPDATE chat_sessions
                        SET current_execution_id = NULL,
                            updated_at = NOW()
                        WHERE id = $1
                          AND current_execution_id = $2
                        """,
                        session_id,
                        UUID(execution_row["execution_id"]),
                    )
                    _partial = execution_row["partial_content"] or ""
                    _tc, _lt = _extract_tool_progress(execution_row["tools_called"])
                    return await _finalize_streaming_status(session_id, {
                        "is_streaming": False,
                        "just_completed": True,
                        "content_length": len(_partial),
                        "token_count": 0,
                        "tool_count": _tc,
                        "last_tool": _lt,
                        "execution_id": execution_row["execution_id"],
                        "last_event_id": execution_row["last_event_id"],
                    }, conn)
                if execution_row["status"] in ("running", "retrying"):
                    _settled = await _settle_stale_execution_for_recovery(
                        conn,
                        session_id,
                        execution_row,
                        has_live_runtime=_has_live_runtime,
                    )
                    if _settled:
                        return await _finalize_streaming_status(session_id, _settled, conn)
                    _partial = execution_row["partial_content"] or ""
                    if not _partial:
                        try:
                            from app.services import redis_stream as _redis_stream
                            _redis_partial, _redis_done = await _redis_stream.reconstruct_from_stream(
                                execution_row["execution_id"]
                            )
                            _redis_clean = svc._strip_streaming_progress_markers(_redis_partial or "")
                            if _redis_clean and svc._has_meaningful_partial_content(_redis_clean):
                                _restored_content = (
                                    _redis_clean
                                    + "\n\n⏳ _생성 중... (재연결 복구 중)_"
                                )
                                _restored_id = await conn.fetchval(
                                    """
                                    INSERT INTO chat_messages (
                                        session_id, execution_id, role, content,
                                        intent, model_used, tools_called,
                                        created_at, edited_at
                                    )
                                    VALUES (
                                        $1, $2, 'assistant', $3,
                                        'streaming_placeholder', 'streaming', '[]'::jsonb,
                                        NOW(), NOW()
                                    )
                                    ON CONFLICT (execution_id)
                                      WHERE intent = 'streaming_placeholder'
                                        AND execution_id IS NOT NULL
                                    DO UPDATE
                                      SET content = EXCLUDED.content,
                                          edited_at = NOW()
                                    RETURNING id
                                    """,
                                    session_id,
                                    UUID(execution_row["execution_id"]),
                                    _restored_content,
                                )
                                await conn.execute(
                                    """
                                    UPDATE chat_turn_executions
                                    SET assistant_message_id = COALESCE(assistant_message_id, $2),
                                        updated_at = NOW()
                                    WHERE id = $1
                                      AND status IN ('running', 'retrying')
                                    """,
                                    UUID(execution_row["execution_id"]),
                                    _restored_id,
                                )
                                await conn.execute(
                                    """
                                    UPDATE chat_sessions
                                    SET updated_at = NOW()
                                    WHERE id = $1
                                    """,
                                    session_id,
                                )
                                _partial = _restored_content
                                logger.warning(
                                    "streaming_status_restored_placeholder_from_redis session=%s execution=%s len=%s done=%s",
                                    str(session_id)[:8],
                                    execution_row["execution_id"][:8],
                                    len(_redis_clean),
                                    _redis_done,
                                )
                        except Exception as _restore_err:
                            logger.warning(
                                "streaming_status_redis_restore_failed session=%s execution=%s error=%s",
                                str(session_id)[:8],
                                execution_row["execution_id"][:8],
                                str(_restore_err)[:160],
                            )
                    if not execution_row["placeholder_id"]:
                        _restored_anchor = await _ensure_running_placeholder_anchor(
                            conn,
                            session_id,
                            execution_row["execution_id"],
                            partial_content=_partial,
                            tools_called=execution_row["tools_called"],
                            last_event_id=execution_row["last_event_id"],
                        )
                        if _restored_anchor:
                            _partial = _restored_anchor["content"] or _partial
                            execution_row = dict(execution_row)
                            execution_row["tools_called"] = _restored_anchor["tools_called"]
                    _tc, _lt = _extract_tool_progress(execution_row["tools_called"])
                    return await _finalize_streaming_status(session_id, {
                        "is_streaming": True,
                        "just_completed": False,
                        "content_length": len(_partial),
                        "tool_count": _tc,
                        "last_tool": _lt,
                        "partial_content": _partial,
                        "execution_id": execution_row["execution_id"],
                        "last_event_id": execution_row["last_event_id"],
                    }, conn)
                await conn.execute(
                    """
                    UPDATE chat_sessions
                    SET current_execution_id = NULL,
                        updated_at = NOW()
                    WHERE id = $1
                      AND current_execution_id = $2
                    """,
                    session_id,
                    UUID(execution_row["execution_id"]),
                )
                _finished_recently = (
                    execution_row["status"] == "completed"
                    and execution_row["updated_recently"]
                )
                if _finished_recently:
                    _tc, _lt = _extract_tool_progress(execution_row["tools_called"])
                    return await _finalize_streaming_status(session_id, {
                        "is_streaming": False,
                        "just_completed": True,
                        "content_length": len(execution_row["partial_content"] or ""),
                        "token_count": 0,
                        "tool_count": _tc,
                        "last_tool": _lt,
                        "execution_id": execution_row["execution_id"],
                        "last_event_id": execution_row["last_event_id"],
                    }, conn)

            row = await conn.fetchrow(
                """
                SELECT id, content, tools_called, execution_id::text AS execution_id
                FROM chat_messages
                WHERE session_id = $1
                  AND intent = 'streaming_placeholder'
                  AND created_at > NOW() - interval '5 minutes'
                ORDER BY created_at DESC LIMIT 1
                """,
                session_id,
            )
            if row:
                if not _has_live_runtime:
                    _settled_placeholder = await _settle_or_surface_orphan_placeholder(
                        conn,
                        session_id,
                        row,
                    )
                    if _settled_placeholder and _settled_placeholder.get("status"):
                        return await _finalize_streaming_status(
                            session_id,
                            _settled_placeholder["status"],
                            conn,
                        )
                _tc, _lt = _extract_tool_progress(row["tools_called"])
                return await _finalize_streaming_status(session_id, {
                    "is_streaming": True,
                    "just_completed": False,
                    "content_length": len(row["content"] or ""),
                    "tool_count": _tc,
                    "last_tool": _lt,
                    "partial_content": row["content"] or "",
                    "execution_id": None,
                    "last_event_id": None,
                }, conn)
            # 5분 초과 stale placeholder 자동 정리
            await conn.execute(
                """
                UPDATE chat_messages
                SET intent = 'interrupted_partial',
                    model_used = 'interrupted',
                    edited_at = NOW()
                WHERE session_id = $1
                  AND intent = 'streaming_placeholder'
                  AND created_at <= NOW() - interval '5 minutes'
                """,
                session_id,
            )
            # 서버 재시작 후 recovered 메시지 감지: 5분 이내 model_used='recovered' 메시지 존재 시
            # just_completed=True, recovered=True 반환 → 클라이언트가 메시지 리로드 수행
            recovered_row = await conn.fetchrow(
                "SELECT id FROM chat_messages"
                " WHERE session_id = $1 AND model_used IN ('recovered', 'recovered_from_redis')"
                "   AND created_at > NOW() - interval '5 minutes'"
                " ORDER BY created_at DESC LIMIT 1",
                session_id,
            )
            if recovered_row:
                logger.info(
                    "streaming_status_recovered_detected session=%s",
                    str(session_id)[:8],
                )
                return await _finalize_streaming_status(session_id, {
                    "is_streaming": False,
                    "just_completed": True,
                    "recovered": True,
                    "content_length": 0,
                    "token_count": 0,
                    "tool_count": 0,
                    "last_tool": "",
                    "execution_id": None,
                    "last_event_id": None,
                }, conn)
            return await _finalize_streaming_status(
                session_id,
                memory_terminal_status or status or {"is_streaming": False},
                conn,
            )
    except Exception as e:
        logger.debug("streaming-status DB 조회 실패", error=str(e), session_id=str(session_id))
    return await _finalize_streaming_status(
        session_id,
        memory_terminal_status or status or {"is_streaming": False},
    )


@router.get("/chat/executions/{execution_id}/events", tags=["chat-session"])
async def execution_events(
    execution_id: UUID,
    last_event_id: Optional[str] = None,
    request: Request = None,
):
    """execution 단위 SSE attach/replay."""
    _last_id = last_event_id
    if not _last_id and request:
        _last_id = request.headers.get("Last-Event-ID")

    from app.services.stream_worker import deliver_sse
    return StreamingResponse(
        deliver_sse(str(execution_id), last_event_id=_last_id or "0"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/chat/sessions/{session_id}/stream-resume", tags=["chat-session"])
async def stream_resume(
    session_id: UUID,
    offset: int = 0,
    message_id: Optional[str] = None,
    last_event_id: Optional[str] = None,
    execution_id: Optional[UUID] = None,
    request: Request = None,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """SSE 재연결: Last-Event-ID 또는 offset 기반으로 끊긴 지점부터 이어서 스트리밍.

    Phase4 워커분리: Redis Stream XREAD blocking으로 실시간 토큰 전달.
    Last-Event-ID가 있으면 Redis Stream에서 해당 지점 이후부터 XREAD.
    없으면 기존 offset 기반 fallback.
    """
    import asyncio
    import json

    sid = str(session_id)
    active_execution = str(execution_id) if execution_id else None
    if not active_execution:
        try:
            current_execution = await svc.get_current_execution(sid, tenant_id=_tenant_id(context))
            if current_execution and current_execution.get("status") in ("running", "retrying"):
                active_execution = str(current_execution["id"])
        except Exception:
            active_execution = None
    if not active_execution:
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                _recent = await conn.fetchval(
                    """
                    SELECT te.id
                      FROM chat_turn_executions te
                      JOIN chat_sessions s ON s.id = te.session_id
                     WHERE te.session_id = $1
                       AND s.tenant_id = $2
                       AND te.created_at > now() - interval '30 minutes'
                     ORDER BY te.created_at DESC
                     LIMIT 1
                    """,
                    session_id,
                    UUID(_tenant_id(context)),
                )
                if _recent:
                    active_execution = str(_recent)
        except Exception:
            pass
    stream_id = active_execution or sid

    # Last-Event-ID 우선: 쿼리 파라미터 → HTTP 헤더
    _last_id = last_event_id
    if not _last_id and request:
        _last_id = request.headers.get("Last-Event-ID")

    # Phase4: Last-Event-ID가 있으면 Redis Stream XREAD 기반 전송
    if _last_id and _last_id != "0":
        from app.services.stream_worker import deliver_sse
        return StreamingResponse(
            deliver_sse(stream_id, last_event_id=_last_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # Fallback: offset 기반 (레거시 호환 + Redis Stream 없는 경우)
    async def _generate():
        prev_len = offset
        while True:
            state = svc._streaming_state.get(sid)
            if not state:
                # Phase4: Redis Stream XREAD로 실시간 복구 시도 (dual-key: execution_id → session_id)
                _keys_to_try = [stream_id] if stream_id == sid else [stream_id, sid]
                for _try_key in _keys_to_try:
                    try:
                        from app.services.stream_worker import deliver_sse
                        _got_data = False
                        async for event in deliver_sse(_try_key, last_event_id="0"):
                            _got_data = True
                            yield event
                        if _got_data:
                            return
                    except Exception:
                        pass
                # Redis 실패 시 DB fallback
                from app.core.db_pool import get_pool
                try:
                    pool = get_pool()
                    async with pool.acquire() as conn:
                        ph_row = await conn.fetchrow(
                            "SELECT content FROM chat_messages WHERE session_id = $1 AND intent = 'streaming_placeholder' AND created_at > NOW() - interval '5 minutes' ORDER BY created_at DESC LIMIT 1",
                            session_id,
                        )
                        if ph_row:
                            yield f"data: {json.dumps({'type': 'resume_generating'})}\n\n"
                            return

                        if active_execution:
                            row = await conn.fetchrow(
                                """
                                SELECT id, content
                                FROM chat_messages
                                WHERE execution_id = $1
                                  AND role = 'assistant'
                                  AND intent IS DISTINCT FROM 'streaming_placeholder'
                                ORDER BY created_at DESC LIMIT 1
                                """,
                                UUID(active_execution),
                            )
                        elif message_id:
                            row = await conn.fetchrow(
                                """
                                SELECT id, content
                                FROM chat_messages
                                WHERE session_id = $1
                                  AND id = $2
                                  AND role = 'assistant'
                                  AND intent IS DISTINCT FROM 'streaming_placeholder'
                                LIMIT 1
                                """,
                                session_id,
                                UUID(message_id),
                            )
                        else:
                            row = None
                        if row and row["content"]:
                            if message_id and str(row["id"]) != message_id:
                                yield f"data: {json.dumps({'type': 'resume_generating'})}\n\n"
                                return
                            remaining = row["content"][prev_len:]
                            if remaining:
                                yield f"data: {json.dumps({'type': 'delta', 'content': remaining})}\n\n"
                        else:
                            yield f"data: {json.dumps({'type': 'resume_unavailable', 'reason': 'no_execution_scoped_response'})}\n\n"
                            return
                except Exception:
                    yield f"data: {json.dumps({'type': 'resume_unavailable', 'reason': 'db_fallback_error'})}\n\n"
                    return
                yield f"data: {json.dumps({'type': 'resume_done'})}\n\n"
                return

            content = state.get("content", "")
            is_done = state.get("completed", False)

            if len(content) > prev_len:
                delta = content[prev_len:]
                prev_len = len(content)
                yield f"data: {json.dumps({'type': 'delta', 'content': delta})}\n\n"

            if is_done:
                yield f"data: {json.dumps({'type': 'resume_done'})}\n\n"
                return

            yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/chat/sessions/{session_id}/last-response", tags=["chat-session"])
async def get_last_response(session_id: UUID):
    """SSE 끊김 시 마지막 AI 응답 복구용.

    클라이언트가 네트워크 끊김 후 서버에서 완성된 응답이 있는지 확인.
    """
    from app.core.db_pool import get_pool
    _has_live_runtime = _has_live_streaming_runtime(session_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        current_execution = await conn.fetchrow(
            """
            SELECT te.id::text AS execution_id,
                   te.status,
                   te.assistant_message_id,
                   te.last_event_id,
                   te.updated_at,
                   EXTRACT(EPOCH FROM (NOW() - te.updated_at))::int AS updated_age_seconds,
                   (te.updated_at > NOW() - interval '5 minutes') AS updated_recently,
                   pm.id::text AS placeholder_id,
                   CASE
                       WHEN te.status IN ('running', 'retrying') THEN COALESCE(pm.content, am.content)
                       ELSE COALESCE(am.content, pm.content)
                   END AS partial_content,
                   CASE
                       WHEN te.status IN ('running', 'retrying') THEN COALESCE(pm.tools_called, am.tools_called)
                       ELSE COALESCE(am.tools_called, pm.tools_called)
                   END AS tools_called
            FROM chat_sessions s
            LEFT JOIN chat_turn_executions te
              ON te.id = COALESCE(
                  s.current_execution_id,
                  (
                      SELECT te_latest.id
                      FROM chat_turn_executions te_latest
                      WHERE te_latest.session_id = s.id
                        AND te_latest.status IN ('running', 'retrying')
                      ORDER BY te_latest.updated_at DESC
                      LIMIT 1
                  )
              )
            LEFT JOIN chat_messages am
              ON am.id = te.assistant_message_id
            LEFT JOIN LATERAL (
                SELECT id, content, tools_called
                FROM chat_messages
                WHERE execution_id = te.id
                  AND intent = 'streaming_placeholder'
                ORDER BY created_at DESC
                LIMIT 1
            ) pm ON TRUE
            WHERE s.id = $1
            """,
            session_id,
        )
        if current_execution and current_execution["status"] in ("running", "retrying"):
            _settled = await _settle_stale_execution_for_recovery(
                conn,
                session_id,
                current_execution,
                has_live_runtime=_has_live_runtime,
            )
            if _settled:
                if _settled.get("auto_retry_scheduled"):
                    return {
                        "found": False,
                        "generating": True,
                        "recovering": True,
                        "execution_id": _settled.get("execution_id"),
                        "last_event_id": _settled.get("last_event_id"),
                    }
                current_execution = None
            else:
                if not current_execution["placeholder_id"]:
                    await _ensure_running_placeholder_anchor(
                        conn,
                        session_id,
                        current_execution["execution_id"],
                        partial_content=current_execution["partial_content"] or "",
                        tools_called=current_execution["tools_called"],
                        last_event_id=current_execution["last_event_id"],
                    )
                return {"found": False, "generating": True}

        if current_execution and current_execution["status"] == "completed":
            await conn.execute(
                """
                UPDATE chat_sessions
                SET current_execution_id = NULL,
                    updated_at = NOW()
                WHERE id = $1
                  AND current_execution_id = $2
                """,
                session_id,
                UUID(current_execution["execution_id"]),
            )

        if current_execution and current_execution["status"] in ("running", "retrying"):
            return {"found": False, "generating": True}

        # streaming_placeholder가 존재하면 보통 생성 중이다. 단, live runtime이
        # 없으면 DB에 저장된 내용을 화면에서 숨기지 말고 보존 응답으로 승격한다.
        placeholder_row = await conn.fetchrow(
            """
            SELECT id, content, tools_called, execution_id::text AS execution_id
            FROM chat_messages
            WHERE session_id = $1
              AND intent = 'streaming_placeholder'
              AND created_at > NOW() - interval '5 minutes'
            ORDER BY created_at DESC LIMIT 1
            """,
            session_id,
        )
        if placeholder_row and _has_live_runtime:
            return {"found": False, "generating": True}
        if placeholder_row:
            _settled_placeholder = await _settle_or_surface_orphan_placeholder(
                conn,
                session_id,
                placeholder_row,
            )
            if _settled_placeholder and _settled_placeholder.get("found"):
                return {
                    "found": True,
                    "generating": False,
                    "message": _settled_placeholder["message"],
                }

        latest_user = await conn.fetchrow(
            """
            SELECT id, created_at
            FROM chat_messages
            WHERE session_id = $1
              AND role = 'user'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            session_id,
        )
        row = await conn.fetchrow(
            """
            SELECT m.id::text, m.content, m.model_used, m.created_at, m.created_at::text AS created_at_text,
                   m.intent, m.execution_id::text AS execution_id
            FROM chat_messages m
            LEFT JOIN chat_turn_executions te
              ON te.id = m.execution_id
            WHERE m.session_id = $1
              AND m.role = 'assistant'
              AND m.intent IS DISTINCT FROM 'streaming_placeholder'
              AND COALESCE(m.intent, '') NOT IN ('auto_reaction', 'system_trigger')
              AND (
                $2::uuid IS NULL
                OR te.user_message_id = $2::uuid
                OR m.created_at > $3::timestamptz
              )
            ORDER BY m.created_at DESC LIMIT 1
            """,
            session_id,
            latest_user["id"] if latest_user else None,
            latest_user["created_at"] if latest_user else None,
        )
    if not row:
        return {"found": False}
    if latest_user and row["created_at"] < latest_user["created_at"]:
        return {"found": False, "generating": False}
    return {
        "found": True,
        "message": {
            "id": row["id"],
            "session_id": str(session_id),
            "role": "assistant",
            "content": row["content"],
            "model_used": row["model_used"],
            "created_at": row["created_at_text"],
            "intent": row["intent"],
            "execution_id": row["execution_id"],
        },
    }


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
    attachments: list[dict] = Field(default_factory=list, description="첨부파일 (이미지/PDF 등)")


@router.post("/chat/sessions/{session_id}/interrupt", tags=["chat-session"])
async def interrupt_session(session_id: UUID, req: InterruptRequest):
    """스트리밍(AI 응답 생성) 중 CEO 추가 지시를 큐에 삽입.

    is_streaming() 상태일 때만 interrupt_queue에 push.
    아닐 때는 일반 메시지 전송 안내 반환.
    도구 루프 완료 시점에 model_selector.py가 has_interrupt() 체크 후 반영.
    AADS-FIX: 인터럽트 메시지를 DB에도 즉시 저장 (유실 방지)
    """
    sid = str(session_id)

    if is_streaming(sid):
        accepts_interrupt = False
        stale_reason = ""
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT te.id::text AS execution_id,
                           te.status,
                           te.last_event_id,
                           EXTRACT(EPOCH FROM (NOW() - te.updated_at))::int AS updated_age_seconds,
                           EXTRACT(EPOCH FROM (NOW() - te.started_at))::int AS started_age_seconds,
                           COALESCE(pm.content, am.content, '') AS partial_content,
                           COALESCE(pm.tools_called, am.tools_called) AS tools_called
                    FROM chat_sessions s
                    LEFT JOIN chat_turn_executions te
                      ON te.id = COALESCE(
                          s.current_execution_id,
                          (
                              SELECT te_latest.id
                              FROM chat_turn_executions te_latest
                              WHERE te_latest.session_id = s.id
                                AND te_latest.status IN ('running', 'retrying')
                              ORDER BY te_latest.updated_at DESC
                              LIMIT 1
                          )
                      )
                    LEFT JOIN chat_messages am
                      ON am.id = te.assistant_message_id
                    LEFT JOIN LATERAL (
                        SELECT content, tools_called
                        FROM chat_messages
                        WHERE execution_id = te.id
                          AND intent = 'streaming_placeholder'
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) pm ON TRUE
                    WHERE s.id = $1
                    """,
                    session_id,
                )
            if row and row["status"] in ("running", "retrying"):
                _clean_partial = svc._strip_streaming_progress_markers(row["partial_content"] or "")
                _tool_count, _last_tool = _extract_tool_progress(row["tools_called"])
                _has_progress = (
                    svc._has_meaningful_partial_content(_clean_partial)
                    or bool(row["last_event_id"])
                    or _tool_count > 0
                )
                _updated_age = int(row["updated_age_seconds"] or 0)
                _started_age = int(row["started_age_seconds"] or 0)
                _empty_stale = not _has_progress and _updated_age >= 150
                _hard_stale = _started_age >= 900 and _updated_age >= 120
                accepts_interrupt = not (_empty_stale or _hard_stale)
                if not accepts_interrupt:
                    stale_reason = (
                        f"stale execution age={_started_age}s updated_age={_updated_age}s "
                        f"tools={_tool_count} last_tool={_last_tool}"
                    )
            else:
                stale_reason = "no running DB execution"
        except Exception as e:
            stale_reason = f"DB execution check failed: {e}"
            accepts_interrupt = False

        if not accepts_interrupt:
            set_streaming(sid, False)
            logger.warning(
                "interrupt_rejected_stale_runtime session_id=%s reason=%s",
                sid,
                stale_reason,
            )
            return {
                "queued": False,
                "message": "현재 실행 중인 AI 응답이 DB 기준으로 없거나 오래되어 일반 메시지로 다시 전송해야 합니다.",
                "reason": stale_reason,
            }

        # DB에 즉시 저장 (유실 방지) — 스트리밍 중일 때만 저장
        try:
            from app.core.db_pool import get_pool
            import json as _json
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO chat_messages
                       (session_id, role, content, attachments)
                       VALUES ($1, 'user', $2, $3::jsonb)""",
                    session_id,
                    f"[추가 지시] {req.content}",
                    _json.dumps(req.attachments or []),
                )
                await conn.execute(
                    "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1",
                    session_id,
                )
            logger.info("interrupt_saved_to_db", session_id=sid, content=req.content[:100])
        except Exception as e:
            logger.error("interrupt_db_save_failed", session_id=sid, error=str(e))

        push_interrupt(sid, req.content, req.attachments if req.attachments else None)
        logger.info("interrupt_queued", session_id=sid, content=req.content[:100],
                     attachments=len(req.attachments))
        return {"queued": True, "message": "추가 지시가 현재 스트림 종료 전 또는 다음 도구 완료 시점에 반영됩니다."}
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
        row = await conn.fetchrow(
            """
            SELECT te.id::text AS execution_id,
                   te.status AS execution_status,
                   te.requested_model,
                   COALESCE(ph.id, te.assistant_message_id) AS placeholder_id,
                   COALESCE(ph.content, am.content, '') AS partial_content,
                   COALESCE(um.content, (
                       SELECT content FROM chat_messages
                       WHERE session_id = s.id AND role = 'user'
                       ORDER BY created_at DESC LIMIT 1
                   )) AS last_user_msg,
                   w.name AS workspace_name
            FROM chat_sessions s
            JOIN chat_workspaces w
              ON w.id = s.workspace_id
            LEFT JOIN chat_turn_executions te
              ON te.id = COALESCE(
                  s.current_execution_id,
                  (
                      SELECT te_latest.id
                      FROM chat_turn_executions te_latest
                      WHERE te_latest.session_id = s.id
                        AND te_latest.status IN ('running', 'retrying')
                      ORDER BY te_latest.updated_at DESC
                      LIMIT 1
                  )
              )
            LEFT JOIN chat_messages am
              ON am.id = te.assistant_message_id
            LEFT JOIN chat_messages um
              ON um.id = te.user_message_id
            LEFT JOIN LATERAL (
                SELECT id, content
                FROM chat_messages
                WHERE execution_id = te.id
                  AND intent = 'streaming_placeholder'
                ORDER BY created_at DESC LIMIT 1
            ) ph ON TRUE
            WHERE s.id = $1
              AND te.status IN ('running', 'retrying')
            LIMIT 1
            """,
            session_id,
        )
        if not row:
            row = await conn.fetchrow("""
                SELECT NULL::text AS execution_id, NULL::text AS execution_status, NULL::text AS requested_model,
                       m.id AS placeholder_id, m.content AS partial_content,
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
            row = await conn.fetchrow("""
                SELECT te.id::text AS execution_id,
                       te.status AS execution_status,
                       te.requested_model,
                       am.id AS placeholder_id,
                       am.content AS partial_content,
                       COALESCE(um.content, (
                           SELECT content FROM chat_messages
                           WHERE session_id = te.session_id AND role = 'user'
                           ORDER BY created_at DESC LIMIT 1
                       )) AS last_user_msg,
                       w.name AS workspace_name
                FROM chat_turn_executions te
                JOIN chat_sessions s ON s.id = te.session_id
                JOIN chat_workspaces w ON w.id = s.workspace_id
                JOIN chat_messages am ON am.id = te.assistant_message_id
                LEFT JOIN chat_messages um ON um.id = te.user_message_id
                WHERE te.session_id = $1
                  AND te.status = 'interrupted'
                  AND am.role = 'assistant'
                  AND am.intent IN (
                    'interrupted_partial',
                    'interruption_notice',
                    'regenerated',
                    'continued',
                    '_archived_partial'
                  )
                  AND length(trim(coalesce(am.content, ''))) > 0
                ORDER BY GREATEST(te.updated_at, COALESCE(am.edited_at, am.created_at)) DESC
                LIMIT 1
            """, session_id)

    if not row:
        return {"resumed": False, "message": "중단된 응답이 없습니다."}

    # F-5: retry_count hard cap + increment for manual resume
    if row["execution_id"]:
        async with pool.acquire() as conn2:
            _cap_row = await conn2.fetchrow(
                "SELECT retry_count, error_message FROM chat_turn_executions WHERE id = $1",
                uuid.UUID(row["execution_id"]),
            )
            _rc = (_cap_row["retry_count"] if _cap_row else 0) or 0
            _err = (_cap_row["error_message"] if _cap_row else "") or ""
            _resume_cap_boundary = _rc == 5 and _err == "execution_resume_attempt_limit_exceeded"
            if _rc > 5 or (_rc >= 5 and not _resume_cap_boundary):
                return {"resumed": False, "message": f"재시도 한도 초과 (retry_count={_rc}). 새 메시지를 보내주세요."}
            await conn2.execute(
                """
                UPDATE chat_turn_executions
                SET retry_count = CASE
                        WHEN retry_count < 5 THEN retry_count + 1
                        ELSE retry_count
                    END,
                    status = 'retrying',
                    completed_at = NULL,
                    error_message = NULL,
                    updated_at = NOW()
                WHERE id = $1
                  AND status IN ('running', 'retrying', 'interrupted')
                """,
                uuid.UUID(row["execution_id"]),
            )
            await conn2.execute(
                """
                UPDATE chat_sessions
                SET current_execution_id = $2,
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_id,
                uuid.UUID(row["execution_id"]),
            )
            if row["placeholder_id"]:
                await conn2.execute(
                    """
                    UPDATE chat_messages
                    SET intent = 'streaming_placeholder',
                        model_used = 'streaming',
                        edited_at = NOW()
                    WHERE id = $1
                      AND role = 'assistant'
                      AND (
                        intent IN (
                          'interrupted_partial',
                          'interruption_notice',
                          'continued',
                          '_archived_partial'
                        )
                        OR model_used = 'interrupted'
                      )
                    """,
                    row["placeholder_id"],
                )

    partial = row["partial_content"] or ""
    clean_partial = re.sub(r'\n\n⏳ _.*?_$', '', partial, flags=re.DOTALL).strip()

    import asyncio
    asyncio.create_task(
        _resume_single_stream(
            sid, row["placeholder_id"], clean_partial,
            row["last_user_msg"] or "", row["workspace_name"] or "CEO",
            execution_id=row["execution_id"],
            requested_model=row["requested_model"],
        )
    )
    return {"resumed": True, "message": "이어서 생성을 시작합니다. 잠시 후 채팅창을 확인하세요."}


@router.post("/chat/messages/{message_id}/regenerate", tags=["chat-message"])
async def regenerate_message(message_id: UUID, request: Request, mode: str = "regenerate"):
    """AI 응답 재생성 또는 이어서 생성. mode=continue: 중단 지점부터 이어서 생성."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        ai_msg = await conn.fetchrow(
            "SELECT id, session_id, role, created_at, content FROM chat_messages WHERE id = $1",
            message_id,
        )
        if not ai_msg:
            raise HTTPException(status_code=404, detail="message not found")
        if ai_msg["role"] != "assistant":
            raise HTTPException(status_code=400, detail="regenerate는 AI 응답에만 사용 가능")

        user_msg = await conn.fetchrow(
            """SELECT id, content, attachments FROM chat_messages
               WHERE session_id = $1 AND created_at < $2 AND role = 'user'
               ORDER BY created_at DESC LIMIT 1""",
            ai_msg["session_id"], ai_msg["created_at"],
        )
        if not user_msg:
            raise HTTPException(status_code=404, detail="이전 사용자 메시지를 찾을 수 없습니다")

        if mode == "continue":
            await conn.execute(
                "UPDATE chat_messages SET intent = 'continued' WHERE id = $1",
                message_id,
            )
        else:
            await conn.execute(
                "UPDATE chat_messages SET intent = 'regenerated' WHERE id = $1",
                message_id,
            )

    session_id_str = str(ai_msg["session_id"])
    content = svc._strip_internal_continuation_context(user_msg["content"])
    import json as _json
    attachments = _json.loads(user_msg["attachments"]) if user_msg["attachments"] else []

    if mode == "continue":
        content = "이어서 진행해"

    from app.services.tool_executor import current_chat_session_id
    current_chat_session_id.set(session_id_str)

    raw_stream = svc.send_message_stream(
        session_id=session_id_str,
        content=content,
        attachments=attachments,
        model_override=None,
        response_mode="quality",
        reply_to_id=str(ai_msg["id"]),
    )
    bg_stream = svc.with_background_completion(raw_stream, session_id=session_id_str)
    return StreamingResponse(
        bg_stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "X-Stream-Session": session_id_str,
        },
    )


@router.put("/chat/messages/{message_id}/bookmark", response_model=MessageOut, tags=["chat-message"])
async def toggle_bookmark(
    message_id: UUID,
    context: TenantContext = Depends(require_tenant_member),
):
    """북마크 토글."""
    result = await svc.toggle_bookmark(str(message_id), tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("message")
    return result


@router.put("/chat/messages/{message_id}", response_model=MessageOut, tags=["chat-message"])
async def update_message(
    message_id: UUID,
    req: MessageUpdateRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """사용자 메시지 내용 수정 (방식A: 수정 후 재전송용)."""
    result = await svc.update_message(str(message_id), req.content, tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("message")
    return result


@router.delete("/chat/messages/{message_id}", tags=["chat-message"])
async def delete_message(
    message_id: UUID,
    context: TenantContext = Depends(require_tenant_member),
):
    """메시지 삭제 + 해당 AI 응답도 함께 삭제 (방식A: 수정재전송 시 기존 응답 제거)."""
    deleted = await svc.delete_message_and_response(str(message_id), tenant_id=_tenant_id(context))
    if not deleted:
        raise _NOT_FOUND("message")
    return {"status": "deleted", "deleted_count": deleted}


@router.get("/chat/messages/search", tags=["chat-message"])
async def search_messages(
    q: str = Query(..., min_length=1),
    workspace_id: Optional[UUID] = Query(None),
    limit: int = Query(20, le=100),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """FTS 전문 검색."""
    results = await svc.search_messages(
        query=q,
        workspace_id=str(workspace_id) if workspace_id else None,
        limit=limit,
        tenant_id=_tenant_id(context),
    )
    return {"messages": results, "total": len(results)}


@router.get("/chat/messages/{message_id}", tags=["chat-message"])
async def get_message_detail(
    message_id: UUID,
    response: Response,
    fields: str = Query("full", pattern="^(full|minimal)$"),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """단일 메시지 상세. fields=minimal 목록에서 도구박스/전체 본문을 lazy hydrate한다."""
    started_at = time.perf_counter()
    result = await svc.get_message(str(message_id), fields=fields, tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("message")
    _set_message_response_headers(response, started_at, result)
    return result


# ─── P2-2: 대화 분기 (Branch) API ─────────────────────────────────────────────

@router.post("/chat/messages/{message_id}/branch", tags=["chat-message"])
async def create_branch(message_id: UUID, req: BranchCreateRequest):
    """특정 메시지 시점에서 새로운 분기 생성 — SSE 스트리밍 응답."""
    import uuid as _uuid
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        # 1) 분기 기준 메시지 조회
        origin_msg = await conn.fetchrow(
            "SELECT id, session_id, role, created_at FROM chat_messages WHERE id = $1",
            message_id,
        )
        if not origin_msg:
            raise HTTPException(status_code=404, detail="메시지를 찾을 수 없습니다")

        session_id_str = str(origin_msg["session_id"])
        branch_id = _uuid.uuid4()

        # 2) 분기점 이전 메시지(자신 포함)만으로 히스토리 구성하여 user 메시지 저장
        await conn.execute(
            """INSERT INTO chat_messages
                (session_id, role, content, model_used, attachments, branch_id, branch_point_id)
            VALUES ($1, 'user', $2, $3, $4::jsonb, $5, $6)""",
            origin_msg["session_id"],
            req.content,
            req.model_override,
            __import__("json").dumps(req.attachments or []),
            branch_id,
            message_id,
        )
        await conn.execute(
            "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1",
            origin_msg["session_id"],
        )

    # 3) send_message_stream 호출 — branch_point 이전 히스토리만 사용
    from app.services.tool_executor import current_chat_session_id
    current_chat_session_id.set(session_id_str)

    raw_stream = svc.send_message_stream(
        session_id=session_id_str,
        content=req.content,
        attachments=req.attachments,
        model_override=req.model_override,
        response_mode=req.response_mode,
        branch_id=str(branch_id),
        branch_point_msg_id=str(message_id),
    )
    bg_stream = svc.with_background_completion(raw_stream, session_id=session_id_str)
    return StreamingResponse(
        bg_stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/sessions/{session_id}/branches", tags=["chat-message"])
async def list_branches(session_id: UUID):
    """세션 내 분기 목록 조회."""
    from app.core.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT branch_id, branch_point_id,
                      MIN(created_at) AS branched_at,
                      (SELECT content FROM chat_messages m2
                       WHERE m2.branch_id = cm.branch_id AND m2.role = 'user'
                       ORDER BY m2.created_at ASC LIMIT 1) AS first_message
               FROM chat_messages cm
               WHERE session_id = $1 AND branch_id IS NOT NULL
               GROUP BY branch_id, branch_point_id
               ORDER BY MIN(created_at) DESC""",
            session_id,
        )
        return [
            {
                "branch_id": str(r["branch_id"]),
                "branch_point_id": str(r["branch_point_id"]),
                "branched_at": r["branched_at"].isoformat() if r["branched_at"] else None,
                "first_message": r["first_message"],
            }
            for r in rows
        ]


# ─── AADS-188D: Diff 승인 API ────────────────────────────────────────────────

_diff_approval_store: dict = {}  # (session_id, tool_use_id) -> action
_DIFF_STORE_MAX = 1000  # 메모리 누수 방지: 최대 항목 수


def _evict_diff_store():
    """1000개 초과 시 오래된 항목 절반 삭제 (삽입 순서 기반, Python 3.7+ dict 보장)."""
    if len(_diff_approval_store) > _DIFF_STORE_MAX:
        keys = list(_diff_approval_store.keys())
        for k in keys[: len(keys) // 2]:
            del _diff_approval_store[k]


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
    _evict_diff_store()
    logger.info("approve_diff", session_id=str(req.session_id), tool_use_id=req.tool_use_id, action=action)
    return ApproveDiffOut(success=True, action=action, message=f"Diff {action} recorded.")


def get_diff_decision(session_id: str, tool_use_id: str) -> Optional[str]:
    """Agent SDK 등에서 승인 여부 조회 (AADS-188D)."""
    return _diff_approval_store.get((session_id, tool_use_id))


# ════════════════════════════════════════════════════════════════════════════════
# Artifact
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/artifacts", response_model=List[ArtifactOut], response_model_by_alias=False, tags=["chat-artifact"])
async def get_artifacts(
    session_id: Optional[UUID] = Query(None),
    workspace_id: Optional[UUID] = Query(None),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """세션 또는 워크스페이스 내 아티팩트 목록."""
    return await svc.list_artifacts(
        session_id=str(session_id) if session_id else None,
        workspace_id=str(workspace_id) if workspace_id else None,
        tenant_id=_tenant_id(context),
    )


@router.get("/chat/artifacts/{artifact_id}", response_model=ArtifactOut, response_model_by_alias=False, tags=["chat-artifact"])
async def get_artifact(
    artifact_id: UUID,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """아티팩트 상세."""
    result = await svc.get_artifact(str(artifact_id), tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("artifact")
    return result


@router.put("/chat/artifacts/{artifact_id}", response_model=ArtifactOut, response_model_by_alias=False, tags=["chat-artifact"])
async def update_artifact(
    artifact_id: UUID,
    req: ArtifactUpdate,
    context: TenantContext = Depends(require_tenant_member),
):
    """아티팩트 수정."""
    result = await svc.update_artifact(str(artifact_id), req.model_dump(exclude_none=True), tenant_id=_tenant_id(context))
    if not result:
        raise _NOT_FOUND("artifact")
    return result


@router.delete("/chat/artifacts/{artifact_id}", tags=["chat-artifact"])
async def delete_artifact(
    artifact_id: UUID,
    context: TenantContext = Depends(require_tenant_member),
):
    """아티팩트 삭제."""
    deleted = await svc.delete_artifact(str(artifact_id), tenant_id=_tenant_id(context))
    if not deleted:
        raise _NOT_FOUND("artifact")
    return {"status": "deleted", "id": str(artifact_id)}


@router.post("/chat/artifacts/{artifact_id}/export", tags=["chat-artifact"])
async def export_artifact(
    artifact_id: UUID,
    req: ArtifactExportRequest,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """아티팩트 내보내기 (pdf/md/html)."""
    result = await svc.export_artifact(str(artifact_id), req.format, tenant_id=_tenant_id(context))
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
    _MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB
    file_bytes = await file.read()
    if len(file_bytes) > _MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"파일 크기 초과: {len(file_bytes)} bytes > {_MAX_UPLOAD_SIZE} bytes (50MB 제한)")
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
# Chat Files (파일 첨부 시스템 Phase 1)
# ════════════════════════════════════════════════════════════════════════════════

@router.post("/chat/files/upload", tags=["chat-files"])
async def upload_chat_file(
    file: UploadFile = File(...),
    session_id: str = Query(...),
    uploaded_by: str = Query("user"),
):
    """파일 업로드 → 디스크 저장 + DB 등록 (이미지는 WebP 압축 + 썸네일 생성)."""
    _MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB
    data = await file.read()
    if len(data) > _MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"파일 크기 초과: {len(data)} bytes > 50MB")
    result = await svc.save_chat_file(session_id, file, data, uploaded_by)
    return result


@router.get("/chat/files/{file_id}", tags=["chat-files"])
async def get_chat_file(file_id: str):
    """파일 다운로드 (원본 또는 압축본)."""
    from fastapi.responses import FileResponse
    from pathlib import Path
    file_info = await svc.get_chat_file(file_id)
    if not file_info:
        raise _NOT_FOUND("file")
    path = Path(file_info["storage_path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="file deleted from disk")
    return FileResponse(
        path,
        media_type=file_info["mime_type"],
        filename=file_info["original_name"],
    )


@router.get("/chat/files/{file_id}/thumbnail", tags=["chat-files"])
async def get_chat_file_thumbnail(file_id: str):
    """썸네일 반환 (이미지만)."""
    from fastapi.responses import FileResponse
    from pathlib import Path
    file_info = await svc.get_chat_file(file_id)
    if not file_info or not file_info.get("thumbnail_path"):
        raise _NOT_FOUND("thumbnail")
    thumb = Path(file_info["thumbnail_path"])
    if not thumb.exists():
        raise HTTPException(status_code=410, detail="thumbnail deleted from disk")
    return FileResponse(thumb, media_type="image/webp")


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


@router.post("/chat/sessions/{session_id}/todos", response_model=ChatTodoItemOut, status_code=201, tags=["chat-todo"])
async def create_session_todo(session_id: UUID, req: ChatTodoCreateRequest):
    """CEO가 직접 TODO 항목을 수동 생성."""
    from app.services.chat_todo_service import create_todo_items
    try:
        rows = await create_todo_items(
            session_id=str(session_id),
            titles=[req.title],
            source="ceo_manual",
            metadata=req.metadata,
        )
        if not rows:
            raise HTTPException(status_code=500, detail="failed to create todo item")
        if req.status and req.status != "pending":
            from app.services.chat_todo_service import update_todo_item
            updated = await update_todo_item(
                todo_id=str(rows[0]["id"]),
                status=req.status,
                source="ceo_manual",
            )
            if updated:
                return updated
        return rows[0]
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("chat_todo_create_failed", session_id=str(session_id), error=str(exc))
        raise HTTPException(status_code=500, detail="failed to create session todo") from exc


@router.get("/chat/sessions/{session_id}/todos", response_model=List[ChatTodoItemOut], tags=["chat-todo"])
async def get_session_todos(
    session_id: UUID,
    include_completed: bool = Query(True),
    cleanup_stale: bool = Query(True, description="오래된 in_progress todo를 pending으로 정리"),
    stale_minutes: int = Query(120, ge=5, le=1440),
):
    """세션별 내부 TODO 하네스 상태를 조회한다."""
    from app.services.chat_todo_service import cleanup_stale_in_progress_todos, list_todo_items

    try:
        if cleanup_stale:
            await cleanup_stale_in_progress_todos(
                session_id=str(session_id),
                stale_after_minutes=stale_minutes,
            )
        return await list_todo_items(
            session_id=str(session_id),
            include_completed=include_completed,
        )
    except Exception as exc:
        logger.warning("chat_todo_list_failed", session_id=str(session_id), error=str(exc))
        raise HTTPException(status_code=500, detail="failed to load session todos") from exc


@router.patch("/chat/sessions/{session_id}/todos/{todo_id}", response_model=ChatTodoItemOut, tags=["chat-todo"])
async def update_session_todo(session_id: UUID, todo_id: UUID, req: ChatTodoUpdateRequest):
    """세션별 TODO 상태/제목을 사용자 액션으로 갱신한다."""
    from app.services.chat_todo_service import update_session_todo_item

    if req.status is None and req.title is None and not req.metadata:
        raise HTTPException(status_code=400, detail="no todo update requested")
    try:
        row = await update_session_todo_item(
            session_id=str(session_id),
            todo_id=str(todo_id),
            status=req.status,
            title=req.title,
            metadata=req.metadata,
            source="chat_ui",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("chat_todo_update_failed", session_id=str(session_id), todo_id=str(todo_id), error=str(exc))
        raise HTTPException(status_code=500, detail="failed to update session todo") from exc
    if not row:
        raise _NOT_FOUND("todo")
    return row


@router.delete("/chat/sessions/{session_id}/todos/{todo_id}", response_model=ChatTodoItemOut, tags=["chat-todo"])
async def delete_session_todo(session_id: UUID, todo_id: UUID):
    """세션별 TODO 한 건을 목록에서 제거한다."""
    from app.services.chat_todo_service import delete_session_todo_item

    try:
        row = await delete_session_todo_item(session_id=str(session_id), todo_id=str(todo_id))
    except Exception as exc:
        logger.warning("chat_todo_delete_failed", session_id=str(session_id), todo_id=str(todo_id), error=str(exc))
        raise HTTPException(status_code=500, detail="failed to delete session todo") from exc
    if not row:
        raise _NOT_FOUND("todo")
    return row


@router.post("/chat/sessions/{session_id}/todos/clear", response_model=ChatTodoBulkActionOut, tags=["chat-todo"])
async def clear_session_todos(session_id: UUID, req: ChatTodoBulkActionRequest):
    """세션별 TODO를 상태 기준으로 일괄 제거한다. 기본은 completed/failed/skipped."""
    from app.services.chat_todo_service import clear_session_todos as clear_items

    try:
        affected = await clear_items(session_id=str(session_id), statuses=req.statuses)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("chat_todo_clear_failed", session_id=str(session_id), error=str(exc))
        raise HTTPException(status_code=500, detail="failed to clear session todos") from exc
    return ChatTodoBulkActionOut(
        affected=affected,
        statuses=req.statuses or ["completed", "failed", "skipped"],
    )


@router.post("/chat/sessions/{session_id}/todos/retry-failed", response_model=ChatTodoBulkActionOut, tags=["chat-todo"])
async def retry_failed_session_todos(session_id: UUID):
    """실패 TODO를 pending으로 되돌려 다음 턴에서 다시 진행할 수 있게 한다."""
    from app.services.chat_todo_service import retry_failed_session_todos as retry_items

    try:
        affected = await retry_items(session_id=str(session_id))
    except Exception as exc:
        logger.warning("chat_todo_retry_failed", session_id=str(session_id), error=str(exc))
        raise HTTPException(status_code=500, detail="failed to retry failed todos") from exc
    return ChatTodoBulkActionOut(affected=affected, statuses=["failed"])


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


# ═══ OAuth 토큰 순서 관리 ═══════════════════════════════════════════════════

@router.get("/settings/auth-keys")
async def get_auth_key_order():
    """현재 인증 키 순서 조회."""
    from app.core.auth_provider import get_oauth_key_records_async

    records = await get_oauth_key_records_async(include_rate_limited=True)
    keys = [
        {
            "label": record.get("label", ""),
            "prefix": record.get("prefix", ""),
            "key_name": record.get("key_name", ""),
            "priority": record.get("priority", 0),
            "slot": record.get("slot", ""),
            "rate_limited_until": record.get("rate_limited_until").isoformat() if record.get("rate_limited_until") else None,
        }
        for record in records
    ]
    return {"keys": keys}


# ════════════════════════════════════════════════════════════════════════════════
# Session Export (대화 내보내기)
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/sessions/{session_id}/export", tags=["chat-session"])
async def export_session(session_id: UUID, format: str = Query("markdown", regex="^(markdown|txt)$")):
    """세션 대화 내보내기 (markdown 또는 txt)."""
    import re
    from datetime import timezone, timedelta
    from app.core.db_pool import get_pool

    KST = timezone(timedelta(hours=9))
    pool = get_pool()

    async with pool.acquire() as conn:
        # 세션 정보 조회
        session = await conn.fetchrow(
            "SELECT id, title, created_at FROM chat_sessions WHERE id = $1", session_id,
        )
        if not session:
            raise _NOT_FOUND("session")

        # 메시지 조회 (streaming_placeholder, regenerated 제외)
        rows = await conn.fetch(
            """SELECT role, content, model_used, created_at
               FROM chat_messages
               WHERE session_id = $1
                 AND intent IS DISTINCT FROM 'streaming_placeholder'
                 AND intent IS DISTINCT FROM 'regenerated'
               ORDER BY created_at ASC""",
            session_id,
        )

    title = session["title"] or "제목 없음"
    from datetime import datetime
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    total = len(rows)

    lines = [
        f"# 대화 내보내기 — {title}",
        f"> 내보내기 일시: {now_kst}",
        f"> 총 메시지: {total}건",
        "",
        "---",
        "",
    ]

    for row in rows:
        ts = row["created_at"]
        if ts.tzinfo is None:
            from datetime import timezone as _tz
            ts = ts.replace(tzinfo=_tz.utc)
        ts_kst = ts.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")

        content = row["content"] or ""
        # thinking_summary 제거 (내부 추론 과정)
        content = re.sub(r'<thinking_summary>.*?</thinking_summary>', '', content, flags=re.DOTALL).strip()
        content = re.sub(r'</?thinking[^>]*>', '', content).strip()

        if row["role"] == "user":
            lines.append(f"## 👤 CEO ({ts_kst})")
        elif row["role"] == "assistant":
            model_tag = f" [모델: {row['model_used']}]" if row["model_used"] else ""
            lines.append(f"## 🤖 AI ({ts_kst}){model_tag}")
        else:
            lines.append(f"## 📌 System ({ts_kst})")

        lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")

    md_content = "\n".join(lines)

    # 파일명 생성 (특수문자 제거)
    safe_title = re.sub(r'[^\w가-힣\s-]', '', title).strip().replace(' ', '_')[:50]
    date_str = datetime.now(KST).strftime("%Y%m%d")
    filename = f"session_{safe_title}_{date_str}.md"

    mime = "text/markdown" if format == "markdown" else "text/plain"
    return Response(
        content=md_content.encode("utf-8"),
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": f"{mime}; charset=utf-8",
        },
    )


# ════════════════════════════════════════════════════════════════════════════════
# Prompt Templates (P2-10)
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/chat/templates", response_model=List[TemplateOut], tags=["chat-template"])
async def list_templates(category: Optional[str] = Query(None)):
    """템플릿 목록 (usage_count DESC 정렬)."""
    return await svc.list_templates(category)


@router.post("/chat/templates", response_model=TemplateOut, status_code=201, tags=["chat-template"])
async def create_template(req: TemplateCreate):
    """새 템플릿 생성."""
    return await svc.create_template(req.model_dump())


@router.delete("/chat/templates/{template_id}", status_code=204, tags=["chat-template"])
async def delete_template(template_id: UUID):
    """템플릿 삭제."""
    ok = await svc.delete_template(str(template_id))
    if not ok:
        raise _NOT_FOUND("template")


@router.post("/chat/templates/{template_id}/use", response_model=TemplateOut, tags=["chat-template"])
async def use_template(template_id: UUID):
    """템플릿 사용 → usage_count 증가."""
    result = await svc.use_template(str(template_id))
    if not result:
        raise _NOT_FOUND("template")
    return result


class KeyOrderRequest(BaseModel):
    primary: str = Field(..., description="우선 사용할 키 label/key_name/slot")


@router.post("/settings/auth-keys")
async def set_auth_key_order(req: KeyOrderRequest):
    """인증 키 순서 변경."""
    from app.core.auth_provider import get_oauth_key_records_async, set_token_order_async

    ok = await set_token_order_async(req.primary)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Unknown key: {req.primary}")
    records = await get_oauth_key_records_async(include_rate_limited=True)
    keys = [
        {
            "label": record.get("label", ""),
            "prefix": record.get("prefix", ""),
            "key_name": record.get("key_name", ""),
            "priority": record.get("priority", 0),
            "slot": record.get("slot", ""),
            "rate_limited_until": record.get("rate_limited_until").isoformat() if record.get("rate_limited_until") else None,
        }
        for record in records
    ]
    return {"ok": True, "keys": keys}
