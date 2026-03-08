"""
AADS-170: CEO Chat-First 시스템 — 채팅 서비스 레이어
DB CRUD, 메시지 전송(SSE 스트리밍), 파일 업로드/다운로드 비즈니스 로직.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import asyncpg
from anthropic import AsyncAnthropic, APIStatusError
from app.config import Settings

logger = logging.getLogger(__name__)

# AADS-186C: Langfuse 트레이스 (optional — graceful degradation)
try:
    from app.core.langfuse_config import create_trace, is_enabled as langfuse_is_enabled
    _LANGFUSE_AVAILABLE = True
except ImportError:
    _LANGFUSE_AVAILABLE = False
    def create_trace(*args, **kwargs): return None  # type: ignore[misc]
    def langfuse_is_enabled() -> bool: return False  # type: ignore[misc]
settings = Settings()

# ─── DB 연결 ──────────────────────────────────────────────────────────────────

def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql://", "postgres://") if url else url


async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_db_url(), timeout=10)


# ─── Anthropic 클라이언트 ──────────────────────────────────────────────────────

_anthropic = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())


# ─── Workspace CRUD ───────────────────────────────────────────────────────────

async def list_workspaces() -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_workspaces ORDER BY created_at"
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def create_workspace(data: Dict[str, Any]) -> Dict[str, Any]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_workspaces (name, system_prompt, files, settings, color, icon)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
            RETURNING *
            """,
            data["name"],
            data.get("system_prompt"),
            json.dumps(data.get("files", [])),
            json.dumps(data.get("settings", {})),
            data.get("color", "#6366F1"),
            data.get("icon", "💬"),
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def update_workspace(workspace_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("name", "system_prompt", "color", "icon"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        for jfield in ("files", "settings"):
            if jfield in data and data[jfield] is not None:
                sets.append(f"{jfield} = ${idx}::jsonb")
                vals.append(json.dumps(data[jfield]))
                idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_workspaces WHERE id = $1", uuid.UUID(workspace_id))
            return _row_to_dict(row) if row else None
        sets.append(f"updated_at = NOW()")
        vals.append(uuid.UUID(workspace_id))
        row = await conn.fetchrow(
            f"UPDATE chat_workspaces SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def delete_workspace(workspace_id: str) -> bool:
    conn = await _get_conn()
    try:
        result = await conn.execute(
            "DELETE FROM chat_workspaces WHERE id = $1", uuid.UUID(workspace_id)
        )
        return result == "DELETE 1"
    finally:
        await conn.close()


# ─── Session CRUD ─────────────────────────────────────────────────────────────

async def list_sessions(workspace_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_sessions WHERE workspace_id = $1 ORDER BY pinned DESC, updated_at DESC LIMIT $2",
            uuid.UUID(workspace_id),
            limit,
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def create_session(data: Dict[str, Any]) -> Dict[str, Any]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions (workspace_id, title)
            VALUES ($1, $2)
            RETURNING *
            """,
            uuid.UUID(str(data["workspace_id"])),
            data.get("title"),
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def update_session(session_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("title", "summary"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        if "pinned" in data and data["pinned"] is not None:
            sets.append(f"pinned = ${idx}")
            vals.append(data["pinned"])
            idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_sessions WHERE id = $1", uuid.UUID(session_id))
            return _row_to_dict(row) if row else None
        sets.append("updated_at = NOW()")
        vals.append(uuid.UUID(session_id))
        row = await conn.fetchrow(
            f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def delete_session(session_id: str) -> bool:
    conn = await _get_conn()
    try:
        result = await conn.execute(
            "DELETE FROM chat_sessions WHERE id = $1", uuid.UUID(session_id)
        )
        return result == "DELETE 1"
    finally:
        await conn.close()


# ─── Message ──────────────────────────────────────────────────────────────────

async def list_messages(session_id: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_messages WHERE session_id = $1 ORDER BY created_at LIMIT $2 OFFSET $3",
            uuid.UUID(session_id),
            limit,
            offset,
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def _save_message(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    role: str,
    content: str,
    model_used: Optional[str] = None,
    intent: Optional[str] = None,
    cost: Decimal = Decimal("0"),
    tokens_in: int = 0,
    tokens_out: int = 0,
    attachments: Optional[List[Any]] = None,
    sources: Optional[List[Any]] = None,
    tools_called: Optional[List[str]] = None,
    thinking_summary: Optional[str] = None,
) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO chat_messages
            (session_id, role, content, model_used, intent, cost, tokens_in, tokens_out,
             attachments, sources, tools_called, thinking_summary)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb, $12)
        RETURNING *
        """,
        session_id,
        role,
        content,
        model_used,
        intent,
        cost,
        tokens_in,
        tokens_out,
        json.dumps(attachments or []),
        json.dumps(sources or []),
        json.dumps(tools_called or []),
        thinking_summary,
    )
    # Update session message count
    await conn.execute(
        "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1",
        session_id,
    )
    return _row_to_dict(row)


async def send_message_stream(
    session_id: str,
    content: str,
    attachments: Optional[List[Any]] = None,
    model_override: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    AADS-185: 3계층 Context Engineering + IntentRouter + ModelSelector + Tool Use 루프.
    SSE 청크: data: {"type": "delta"|"thinking"|"tool_use"|"tool_result"|"done"|"error", ...}
    """
    conn = await _get_conn()
    # AADS-186C: Langfuse 트레이스 시작
    _lf_trace = create_trace(
        name="chat_turn",
        session_id=session_id,
        user_id="CEO",
        input_data={"content": content[:500], "model_override": model_override},
    )
    _lf_span_intent = None
    _lf_span_llm = None
    _trace_start_time = __import__("time").monotonic()

    try:
        sid = uuid.UUID(session_id)

        # 1. 사용자 메시지 저장
        await _save_message(conn, sid, "user", content, attachments=attachments or [])

        # 2. 워크스페이스 정보 조회
        sp_row = await conn.fetchrow(
            """
            SELECT w.id::text AS workspace_id, w.system_prompt, w.name AS workspace_name
            FROM chat_workspaces w
            JOIN chat_sessions s ON s.workspace_id = w.id
            WHERE s.id = $1
            """,
            sid,
        )
        base_prompt = (sp_row["system_prompt"] if sp_row and sp_row["system_prompt"] else "")
        workspace_name = (sp_row["workspace_name"] if sp_row and sp_row["workspace_name"] else "CEO")

        # 3. 세션 히스토리 조회 (최근 25개)
        hist_rows = await conn.fetch(
            """
            SELECT role, content FROM chat_messages
            WHERE session_id = $1 AND (is_compacted IS NULL OR is_compacted = false)
            ORDER BY created_at DESC LIMIT 25
            """,
            sid,
        )
        raw_messages = [{"role": r["role"], "content": r["content"]} for r in reversed(hist_rows)]

        # 4. 3계층 컨텍스트 빌드
        from app.services.context_builder import build_messages_context
        messages, system_prompt = await build_messages_context(
            workspace_name=workspace_name,
            session_id=session_id,
            raw_messages=raw_messages,
            base_system_prompt=base_prompt,
            db_conn=conn,
        )

        # 5. 자동 압축 (20턴 초과 시)
        from app.services.compaction_service import check_and_compact
        messages = await check_and_compact(session_id, messages, db_conn=conn)

        # 6. 인텐트 분류 + 모델/도구 결정
        from app.services.intent_router import classify, get_model_for_override
        intent_result = await classify(content, workspace_name)
        intent = intent_result.intent
        # Langfuse: intent_classification span
        if _lf_trace is not None:
            try:
                _lf_span_intent = _lf_trace.span(
                    name="intent_classification",
                    input={"content": content[:300], "workspace": workspace_name},
                    output={"intent": intent, "model": intent_result.model, "use_tools": intent_result.use_tools},
                    metadata={"use_gemini_direct": intent_result.use_gemini_direct},
                )
                if _lf_span_intent:
                    _lf_span_intent.end()
            except Exception:
                pass

        if model_override:
            intent_result.model = get_model_for_override(model_override)
            intent_result.use_gemini_direct = False

        # 7. Gemini Direct (Grounding / Deep Research)
        if intent_result.use_gemini_direct:
            if intent_result.gemini_mode == "grounding":
                from app.services.gemini_search_service import GeminiSearchService
                svc = GeminiSearchService()
                result = None
                try:
                    result = await svc.search_grounded(content)
                except Exception as e:
                    logger.warning(f"gemini_grounding_failed: {e}")
                if result is None:
                    from app.services.brave_search_service import BraveSearchService
                    brave = BraveSearchService()
                    result = await brave.search(content)
                yield f"data: {json.dumps({'type': 'delta', 'content': result.text})}\n\n"
                if result.citations:
                    yield f"data: {json.dumps({'type': 'sources', 'sources': result.citations})}\n\n"
                await _save_message(conn, sid, "assistant", result.text,
                    model_used="gemini-flash", intent=intent, cost=Decimal("0"), sources=result.citations)
                await conn.execute(
                    "UPDATE chat_sessions SET updated_at = NOW() WHERE id = $1", sid)
                yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': 'gemini-flash', 'cost': '0'})}\n\n"
                return

            elif intent_result.gemini_mode == "deep_research":
                from app.services.gemini_research_service import GeminiResearchService
                svc = GeminiResearchService()
                try:
                    async for event in svc.start_research_stream(content, session_id, conn):
                        yield f"data: {json.dumps(event)}\n\n"
                    return
                except Exception as e:
                    logger.warning(f"gemini_deep_research_failed: {e}")
                    intent_result.model = "claude-sonnet"
                    intent_result.use_gemini_direct = False

        # 8. 도구 목록 (Anthropic Tool Use 포맷)
        tools_for_api = None
        if intent_result.use_tools:
            from app.services.tool_registry import ToolRegistry
            tools_for_api = ToolRegistry().get_tools(intent_result.tool_group)

        # 9. 모델 선택기 → SSE 스트리밍
        from app.services.model_selector import call_stream
        # Langfuse: llm_generation span 시작
        if _lf_trace is not None:
            try:
                _lf_span_llm = _lf_trace.span(
                    name="llm_generation",
                    input={"model": intent_result.model, "intent": intent},
                )
            except Exception:
                pass
        full_response = ""
        thinking_summary = ""
        model_used = intent_result.model
        cost_usd = Decimal("0")
        input_tokens = 0
        output_tokens = 0
        tools_called: list = []

        async for event in call_stream(
            intent_result=intent_result,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools_for_api,
            model_override=model_override,
        ):
            etype = event.get("type", "")
            if etype == "delta":
                full_response += event.get("content", "")
                yield f"data: {json.dumps({'type': 'delta', 'content': event['content']})}\n\n"
            elif etype == "thinking":
                thinking_summary += event.get("thinking", "")
                yield f"data: {json.dumps({'type': 'thinking', 'thinking': event['thinking']})}\n\n"
            elif etype == "tool_use":
                tools_called.append(event["tool_name"])
                yield f"data: {json.dumps({'type': 'tool_use', 'tool_name': event['tool_name'], 'tool_use_id': event['tool_use_id']})}\n\n"
            elif etype == "tool_result":
                yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': event['tool_name'], 'content': str(event.get('content', ''))[:500]})}\n\n"
            elif etype == "done":
                model_used = event.get("model", intent_result.model)
                cost_usd = Decimal(str(event.get("cost", "0")))
                input_tokens = event.get("input_tokens", 0) or 0
                output_tokens = event.get("output_tokens", 0) or 0
                thinking_summary = event.get("thinking_summary") or thinking_summary
                tools_called = event.get("tools_called", tools_called)
            elif etype == "error":
                yield f"data: {json.dumps({'type': 'error', 'content': event.get('content', '오류')})}\n\n"
                return

        # 10. 응답 저장
        await _save_message(
            conn, sid, "assistant", full_response,
            model_used=model_used,
            intent=intent,
            cost=cost_usd,
            tokens_in=input_tokens,
            tokens_out=output_tokens,
            sources=[],
            tools_called=tools_called,
            thinking_summary=thinking_summary or None,
        )
        await conn.execute(
            "UPDATE chat_sessions SET cost_total = cost_total + $1, updated_at = NOW() WHERE id = $2",
            cost_usd, sid,
        )

        yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': model_used, 'cost': str(cost_usd), 'input_tokens': input_tokens, 'output_tokens': output_tokens, 'thinking_summary': (thinking_summary[:300] if thinking_summary else None)})}\n\n"

    finally:
        await conn.close()


async def toggle_bookmark(message_id: str) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "UPDATE chat_messages SET bookmarked = NOT bookmarked WHERE id = $1 RETURNING *",
            uuid.UUID(message_id),
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def search_messages(query: str, workspace_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        if workspace_id:
            rows = await conn.fetch(
                """
                SELECT m.* FROM chat_messages m
                JOIN chat_sessions s ON s.id = m.session_id
                WHERE s.workspace_id = $1
                  AND to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $2)
                ORDER BY m.created_at DESC LIMIT $3
                """,
                uuid.UUID(workspace_id),
                query,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM chat_messages
                WHERE to_tsvector('simple', content) @@ plainto_tsquery('simple', $1)
                ORDER BY created_at DESC LIMIT $2
                """,
                query,
                limit,
            )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


# ─── Artifact ────────────────────────────────────────────────────────────────

async def list_artifacts(session_id: str) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_artifacts WHERE session_id = $1 ORDER BY created_at",
            uuid.UUID(session_id),
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def get_artifact(artifact_id: str) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM chat_artifacts WHERE id = $1",
            uuid.UUID(artifact_id),
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def update_artifact(artifact_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("title", "content"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        if "metadata" in data and data["metadata"] is not None:
            sets.append(f"metadata = ${idx}::jsonb")
            vals.append(json.dumps(data["metadata"]))
            idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_artifacts WHERE id = $1", uuid.UUID(artifact_id))
            return _row_to_dict(row) if row else None
        sets.append("updated_at = NOW()")
        vals.append(uuid.UUID(artifact_id))
        row = await conn.fetchrow(
            f"UPDATE chat_artifacts SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def export_artifact(artifact_id: str, fmt: str) -> Dict[str, Any]:
    """단순 텍스트 내보내기. PDF는 향후 확장."""
    artifact = await get_artifact(artifact_id)
    if not artifact:
        return {}
    content = artifact["content"]
    if fmt == "md":
        body = f"# {artifact.get('title', 'Artifact')}\n\n{content}"
        mime = "text/markdown"
    elif fmt == "html":
        body = f"<html><body><h1>{artifact.get('title', 'Artifact')}</h1><pre>{content}</pre></body></html>"
        mime = "text/html"
    else:
        # pdf: 텍스트로 반환 (실제 PDF 변환은 별도 라이브러리 필요)
        body = content
        mime = "application/pdf"
    return {"content": body, "mime": mime, "filename": f"artifact_{artifact_id}.{fmt}"}


# ─── Drive ───────────────────────────────────────────────────────────────────

UPLOAD_DIR = Path(os.getenv("CHAT_UPLOAD_DIR", "/root/aads/uploads/chat"))


async def list_drive_files(workspace_id: str) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_drive_files WHERE workspace_id = $1 ORDER BY created_at DESC",
            uuid.UUID(workspace_id),
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def save_drive_file(
    workspace_id: str,
    filename: str,
    file_bytes: bytes,
    file_type: Optional[str],
    uploaded_by: str = "user",
) -> Dict[str, Any]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4()}_{filename}"
    file_path = UPLOAD_DIR / safe_name
    file_path.write_bytes(file_bytes)

    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_drive_files (workspace_id, filename, file_path, file_type, file_size, uploaded_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            uuid.UUID(workspace_id),
            filename,
            str(file_path),
            file_type,
            len(file_bytes),
            uploaded_by,
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def delete_drive_file(file_id: str) -> bool:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "DELETE FROM chat_drive_files WHERE id = $1 RETURNING file_path",
            uuid.UUID(file_id),
        )
        if not row:
            return False
        path = Path(row["file_path"])
        if path.exists():
            path.unlink(missing_ok=True)
        return True
    finally:
        await conn.close()


async def get_drive_file(file_id: str) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM chat_drive_files WHERE id = $1",
            uuid.UUID(file_id),
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


# ─── Research Archive ────────────────────────────────────────────────────────

async def get_research_cache(topic: str, days: int = 7) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            SELECT * FROM research_archive
            WHERE topic ILIKE $1
              AND created_at >= NOW() - ($2 || ' days')::INTERVAL
            ORDER BY created_at DESC LIMIT 1
            """,
            f"%{topic}%",
            str(days),
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def list_research_history(limit: int = 50) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM research_archive ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


# ─── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _row_to_dict(row: asyncpg.Record) -> Dict[str, Any]:
    """asyncpg Record → Python dict. JSONB 문자열 파싱 포함."""
    if row is None:
        return {}
    result = {}
    for key in row.keys():
        val = row[key]
        # asyncpg는 JSONB를 문자열로 반환하는 경우가 있음 → 배열/객체 모두 파싱
        if isinstance(val, str) and len(val) >= 2 and val[0] in ("{", "["):
            try:
                val = json.loads(val)
            except Exception:
                pass
        result[key] = val
    return result
