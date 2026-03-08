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
) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO chat_messages
            (session_id, role, content, model_used, intent, cost, tokens_in, tokens_out, attachments, sources)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb)
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
    사용자 메시지 저장 → 인텐트 분류 → Claude SSE 스트리밍 응답 → 응답 저장.
    각 SSE 청크: data: {"type": "delta"|"done"|"error", "content": "..."}
    """
    conn = await _get_conn()
    try:
        sid = uuid.UUID(session_id)

        # 1. 사용자 메시지 저장
        await _save_message(conn, sid, "user", content, attachments=attachments or [])

        # 2. 인텐트 분류 (ceo_chat.py 의 classify_intent 재사용)
        try:
            from app.api.ceo_chat import classify_intent
            intent = classify_intent(content)
        except Exception:
            intent = "strategy"

        # 3. 모델 결정
        model = model_override or "claude-sonnet-4-6"

        # 4. 세션 히스토리 조회 (최근 10개)
        hist_rows = await conn.fetch(
            """
            SELECT role, content FROM chat_messages
            WHERE session_id = $1
            ORDER BY created_at DESC LIMIT 10
            """,
            sid,
        )
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(hist_rows)]

        # system_prompt + 워크스페이스 이름/ID 조회
        sp_row = await conn.fetchrow(
            """
            SELECT w.id::text AS workspace_id, w.system_prompt, w.name AS workspace_name
            FROM chat_workspaces w
            JOIN chat_sessions s ON s.workspace_id = w.id
            WHERE s.id = $1
            """,
            sid,
        )
        base_prompt = (sp_row["system_prompt"] if sp_row and sp_row["system_prompt"]
                       else "당신은 CEO 전용 AI 어시스턴트입니다.")
        workspace_name = (sp_row["workspace_name"] if sp_row and sp_row["workspace_name"] else "")
        workspace_id_str = (sp_row["workspace_id"] if sp_row and sp_row["workspace_id"] else "")

        # AADS-183: 컨텍스트 풍부화 — HANDOVER 정보 + 날짜 + 도구 정보 주입
        from app.services.context_builder import build_system_context
        injected_context = build_system_context(workspace_name)
        system_prompt = injected_context + "---\n" + base_prompt

        # AADS-184: 인텐트→도구 호출→결과 주입
        tool_result_str = ""
        sources_data: list = []
        try:
            from app.services.tool_executor import execute_tools, build_tool_injection
            tool_result_str = await execute_tools(intent, content, workspace_id_str)
            if tool_result_str:
                sources_data = [{"tool_result": tool_result_str[:500]}]
                logger.info(f"chat_tool_executed: intent={intent} result_chars={len(tool_result_str)}")
        except Exception as _tool_err:
            logger.warning(f"chat_tool_execution_failed: intent={intent} error={_tool_err}")
            tool_result_str = ""

        # 도구 결과를 메시지로 주입 (있을 때만)
        tool_injection = ""
        if tool_result_str:
            from app.services.tool_executor import build_tool_injection
            tool_injection = build_tool_injection(tool_result_str)

        # 메시지 구성: 히스토리 + 도구 결과 주입
        # history[-1]이 현재 user 메시지 — 도구 결과를 해당 메시지에 합산
        # (Anthropic API: 연속 user 메시지 불가 → 합산 방식 사용)
        messages_payload = list(history)
        if tool_injection and messages_payload and messages_payload[-1]["role"] == "user":
            # 현재 user 메시지에 도구 결과 합산
            messages_payload[-1] = {
                "role": "user",
                "content": messages_payload[-1]["content"] + "\n\n" + tool_injection,
            }

        # 5. SSE 스트리밍
        full_response = ""
        input_tokens = 0
        output_tokens = 0
        cost_usd = Decimal("0")
        # fallback 접두사: 도구 실패 시
        _fallback_prefix = ""
        if intent not in ("casual", "strategy", "planning", "decision", "design",
                          "design_fix", "architect", "code_exec", "browser",
                          "image_analyze", "video_analyze") and not tool_result_str:
            try:
                from app.services.tool_executor import has_tools_for_intent
                if has_tools_for_intent(intent):
                    _fallback_prefix = "현재 도구 조회가 실패하여 제한된 정보로 답변합니다.\n\n"
            except Exception:
                pass

        try:
            async with _anthropic.messages.stream(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages_payload,
            ) as stream:
                # fallback 접두사 먼저 스트리밍
                if _fallback_prefix:
                    full_response += _fallback_prefix
                    yield f"data: {json.dumps({'type': 'delta', 'content': _fallback_prefix})}\n\n"

                async for text in stream.text_stream:
                    full_response += text
                    yield f"data: {json.dumps({'type': 'delta', 'content': text})}\n\n"

                final_msg = await stream.get_final_message()
                input_tokens = final_msg.usage.input_tokens
                output_tokens = final_msg.usage.output_tokens
                # 비용 추정 (claude-sonnet-4-6 기준 $3/$15 per 1M)
                cost_usd = Decimal(str(round(
                    input_tokens * 3e-6 + output_tokens * 15e-6, 6
                )))

        except APIStatusError as e:
            logger.error(f"chat_stream_api_error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            return

        # 6. 어시스턴트 응답 저장 (sources: 도구 호출 결과 JSON)
        await _save_message(
            conn, sid, "assistant", full_response,
            model_used=model,
            intent=intent,
            cost=cost_usd,
            tokens_in=input_tokens,
            tokens_out=output_tokens,
            sources=sources_data if sources_data else [],
        )
        # 세션 비용 합산
        await conn.execute(
            "UPDATE chat_sessions SET cost_total = cost_total + $1, updated_at = NOW() WHERE id = $2",
            cost_usd,
            sid,
        )

        yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': model, 'cost': str(cost_usd)})}\n\n"

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
