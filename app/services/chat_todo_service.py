from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

TODO_STATUS_PENDING = "pending"
TODO_STATUS_IN_PROGRESS = "in_progress"
TODO_STATUS_COMPLETED = "completed"
TODO_STATUS_FAILED = "failed"
TODO_STATUS_SKIPPED = "skipped"

TODO_ACTIVE_STATUSES = (TODO_STATUS_PENDING, TODO_STATUS_IN_PROGRESS)
TODO_TERMINAL_STATUSES = (
    TODO_STATUS_COMPLETED,
    TODO_STATUS_FAILED,
    TODO_STATUS_SKIPPED,
)
TODO_VALID_STATUSES = set(TODO_ACTIVE_STATUSES) | set(TODO_TERMINAL_STATUSES)
DEFAULT_STALE_IN_PROGRESS_MINUTES = 120

_ACTION_HINTS = (
    "확인", "점검", "검증", "분석", "조사", "수정", "추가", "삭제", "생성", "작성",
    "적용", "반영", "정리", "보고", "통합", "연동", "기록", "실행", "비교", "저장",
)
_MULTI_STEP_MARKERS = (
    "1.", "2.", "3.", "4.", "5.", "그리고", "다음", "먼저", "이어서", "각각", "순서대로", "완료 조건",
)
_FOLLOWUP_MARKERS = ("계속", "이어서", "남은", "진행해", "후속", "마저", "계속해")
_FAILURE_MARKERS = ("실패", "오류", "에러", "못했습니다", "불가", "중단", "재시도")
_GENERIC_STOPWORDS = {
    "및", "후", "전", "관련", "기존", "추가", "수정", "점검", "확인", "정리", "보고",
    "the", "and", "for", "with", "from", "into", "this", "that",
}
_HEADING_PREFIXES = (
    "목표",
    "완료 조건",
    "검증",
    "변경 사항",
    "변경사항",
    "필수 규칙",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _normalize_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _normalize_todo_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = _normalize_metadata(item.get("metadata"))
    return item


def _append_audit(
    metadata: Optional[dict[str, Any]],
    *,
    action: str,
    source: str,
    detail: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = _normalize_metadata(metadata)
    audit = payload.get("audit")
    if not isinstance(audit, list):
        audit = []
    entry = {
        "action": action,
        "source": source,
        "at": _utcnow().isoformat(),
    }
    if detail:
        entry.update(detail)
    audit.append(entry)
    payload["audit"] = audit[-20:]
    return payload


def _looks_like_question(message: str) -> bool:
    text = (message or "").strip()
    return text.endswith("?") or text.endswith("요?") or text.endswith("인가") or text.endswith("인가요")


def should_create_todos(message: str, *, intent: str, use_tools: bool) -> bool:
    text = (message or "").strip()
    if len(text) < 6:
        return False
    if intent in {"greeting", "casual"} and not use_tools:
        return False
    if any(marker in text for marker in ("안녕", "고마워", "감사", "수고", "반가워")) and not use_tools:
        return False
    if _looks_like_question(text):
        return False
    if sum(1 for marker in _MULTI_STEP_MARKERS if marker in text) >= 2:
        return True
    if re.search(r"(^|\s)[0-9]+\.\s", text):
        return True
    if use_tools and len(text) >= 8:
        return True
    return any(hint in text for hint in _ACTION_HINTS) and any(marker in text for marker in ("그리고", "다음", "먼저", "\n", ";"))


def should_resume_session_todos(message: str) -> bool:
    text = (message or "").strip()
    return bool(text) and any(marker in text for marker in _FOLLOWUP_MARKERS)


def _insert_task_breaks(message: str) -> str:
    text = (message or "").replace("\r\n", "\n")
    text = re.sub(r"(?<!^)\s(?=[0-9]+\.\s)", "\n", text)
    text = re.sub(r"(?<!^)\s(?=[-*\u2022]\s)", "\n", text)
    return text


def _clean_candidate(raw: str) -> str:
    candidate = (raw or "").strip()
    candidate = re.sub(r"^[0-9]+\.\s*", "", candidate)
    candidate = re.sub(r"^[-*\u2022]\s*", "", candidate)
    candidate = candidate.strip(" .:-")
    if ":" in candidate:
        prefix, suffix = candidate.split(":", 1)
        if prefix.strip() in _HEADING_PREFIXES and suffix.strip():
            candidate = suffix.strip()
    if candidate in _HEADING_PREFIXES:
        return ""
    return re.sub(r"\s+", " ", candidate).strip()


def _split_clause_candidates(message: str) -> list[str]:
    text = _insert_task_breaks(message)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[str] = []
    for line in lines:
        cleaned = _clean_candidate(line)
        if not cleaned or len(cleaned) < 4:
            continue
        candidates.append(cleaned)
    if candidates:
        return candidates

    fragments = re.split(r"(?:\n+|[;])", text)
    for fragment in fragments:
        fragment = fragment.strip()
        if not fragment:
            continue
        if " 그리고 " in fragment:
            candidates.extend(part.strip() for part in fragment.split(" 그리고 ") if part.strip())
        else:
            candidates.append(fragment)
    return [_clean_candidate(item) for item in candidates if _clean_candidate(item)]


def _derive_match_terms(title: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9가-힣_/-]+", title or ""):
        normalized = token.lower().strip()
        if len(normalized) < 2 or normalized in _GENERIC_STOPWORDS:
            continue
        if normalized not in terms:
            terms.append(normalized)
    return terms[:6]


def extract_todo_titles(message: str, *, intent: str, use_tools: bool, max_items: int = 8) -> list[str]:
    raw_candidates = _split_clause_candidates(message)
    titles: list[str] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        cleaned = _clean_candidate(candidate)
        if not cleaned or len(cleaned) < 4:
            continue
        key = _normalize_text(cleaned)
        if key in seen:
            continue
        if key.startswith("목표 ") or key.startswith("완료 조건 "):
            continue
        seen.add(key)
        titles.append(cleaned[:160])
        if len(titles) >= max_items:
            return titles

    if titles:
        return titles

    if not use_tools:
        return []

    fallback = re.split(r"[.!?\n]", (message or "").strip(), maxsplit=1)[0].strip()
    fallback = _clean_candidate(fallback)
    if not fallback:
        fallback = "요청 확인 및 결과 보고"
    return [fallback[:160]]


def build_todo_prompt_block(todo_items: Iterable[dict[str, Any]]) -> str:
    items = list(todo_items)
    if not items:
        return ""
    lines = [
        "",
        "[세션 TODO 운영 규칙]",
        "- 이 요청은 복수 작업 또는 실행형 요청으로 분류되었다.",
        "- 아래 TODO 순서를 기준으로 진행하고, 처리하지 않은 항목은 완료로 단정하지 마라.",
        "- 최종 응답 전 TODO 누락 여부를 다시 점검하라.",
    ]
    for index, item in enumerate(items[:8], start=1):
        lines.append(f"{index}. {item.get('title', '')}")
    return "\n".join(lines)


def _response_matches_terms(response_text: str, title: str, match_terms: list[str]) -> bool:
    if not response_text:
        return False
    normalized_title = _normalize_text(title)
    if normalized_title and normalized_title in response_text:
        return True
    if not match_terms:
        return False
    hits = sum(1 for term in match_terms if term in response_text)
    if len(match_terms) == 1:
        return hits >= 1
    return hits >= min(2, len(match_terms))


def response_indicates_failure(response_text: str) -> bool:
    normalized = _normalize_text(response_text)
    return bool(normalized) and any(marker in normalized for marker in _FAILURE_MARKERS)


def evaluate_todo_completion(
    todo_items: Iterable[dict[str, Any]],
    *,
    response_text: str,
    tools_called: Optional[Iterable[dict[str, Any]]] = None,
) -> dict[str, Any]:
    items = list(todo_items)
    normalized_response = _normalize_text(response_text)
    tool_names = {
        str(item.get("tool_name") or "").strip()
        for item in (tools_called or [])
        if isinstance(item, dict) and item.get("type") == "tool_use"
    }
    generic_success = bool(normalized_response) and not response_indicates_failure(normalized_response)
    completed_ids: list[str] = []
    missing_items: list[dict[str, Any]] = []

    for item in items:
        metadata = _normalize_metadata(item.get("metadata"))
        match_terms = metadata.get("match_terms")
        if not isinstance(match_terms, list):
            match_terms = _derive_match_terms(str(item.get("title") or ""))
        matched = _response_matches_terms(normalized_response, str(item.get("title") or ""), match_terms)
        if not matched and metadata.get("is_generic") and generic_success:
            matched = True
        if not matched and metadata.get("requires_tool") and tool_names and generic_success:
            matched = True
        if matched:
            completed_ids.append(str(item.get("id")))
        else:
            missing_items.append(dict(item))

    return {
        "completed_ids": completed_ids,
        "missing_items": missing_items,
        "missing_titles": [str(item.get("title") or "") for item in missing_items],
        "all_completed": not missing_items,
        "has_failure_signal": response_indicates_failure(normalized_response),
    }


def build_missing_todo_note(missing_titles: Iterable[str]) -> str:
    titles = [title.strip() for title in missing_titles if title and title.strip()]
    if not titles:
        return ""
    lines = ["", "[세션 TODO 점검]", "미완료 항목:"]
    for title in titles[:8]:
        lines.append(f"- {title}")
    return "\n".join(lines)


async def ensure_chat_todo_schema(conn: Any | None = None) -> None:
    async def _ensure(active_conn: Any) -> None:
        await active_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_todo_items (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                message_id UUID NULL REFERENCES chat_messages(id) ON DELETE SET NULL,
                execution_id UUID NULL REFERENCES chat_turn_executions(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                sort_order INTEGER NOT NULL DEFAULT 0,
                source VARCHAR(50) NOT NULL DEFAULT 'user_turn',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ NULL
            )
            """
        )
        await active_conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_todo_items_session_status_sort
            ON chat_todo_items(session_id, status, sort_order, updated_at DESC)
            """
        )
        await active_conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_todo_items_execution
            ON chat_todo_items(execution_id, sort_order)
            WHERE execution_id IS NOT NULL
            """
        )
        await active_conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_todo_items_message
            ON chat_todo_items(message_id, sort_order)
            WHERE message_id IS NOT NULL
            """
        )
        await active_conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_todo_items_turn_order
            ON chat_todo_items(session_id, execution_id, source, sort_order)
            WHERE execution_id IS NOT NULL
            """
        )
        await active_conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_todo_items_message_order
            ON chat_todo_items(session_id, message_id, source, sort_order)
            WHERE message_id IS NOT NULL
            """
        )

    if conn is not None:
        await _ensure(conn)
        return

    pool = get_pool()
    async with pool.acquire() as active_conn:
        await _ensure(active_conn)


async def create_todo_items(
    *,
    session_id: str,
    titles: Iterable[str],
    message_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    source: str = "user_turn",
    metadata: Optional[dict[str, Any]] = None,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    sid = uuid.UUID(str(session_id))
    mid = uuid.UUID(str(message_id)) if message_id else None
    eid = uuid.UUID(str(execution_id)) if execution_id else None
    prepared_titles = [title.strip() for title in titles if title and title.strip()]
    if not prepared_titles:
        return []

    async def _create(active_conn: Any) -> list[dict[str, Any]]:
        if eid:
            await active_conn.execute(
                "DELETE FROM chat_todo_items WHERE session_id = $1 AND execution_id = $2 AND source = $3",
                sid,
                eid,
                source,
            )
        elif mid:
            await active_conn.execute(
                "DELETE FROM chat_todo_items WHERE session_id = $1 AND message_id = $2 AND source = $3",
                sid,
                mid,
                source,
            )

        rows: list[dict[str, Any]] = []
        for sort_order, title in enumerate(prepared_titles):
            item_metadata = _normalize_metadata(metadata)
            item_metadata.setdefault("match_terms", _derive_match_terms(title))
            item_metadata.setdefault("requires_tool", bool(item_metadata.get("requires_tool")))
            item_metadata.setdefault("is_generic", len(prepared_titles) == 1 and bool(item_metadata.get("requires_tool")))
            item_metadata = _append_audit(
                item_metadata,
                action="create",
                source=source,
                detail={"title": title},
            )
            row = await active_conn.fetchrow(
                """
                INSERT INTO chat_todo_items (
                    session_id, message_id, execution_id, title, status, sort_order, source, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                RETURNING *
                """,
                sid,
                mid,
                eid,
                title,
                TODO_STATUS_IN_PROGRESS if sort_order == 0 else TODO_STATUS_PENDING,
                sort_order,
                source,
                json.dumps(item_metadata, ensure_ascii=False),
            )
            rows.append(_normalize_todo_row(row))
        logger.info(
            "chat_todo_created session=%s execution=%s count=%s source=%s",
            str(session_id)[:8],
            str(execution_id or "")[:8],
            len(rows),
            source,
        )
        return rows

    if conn is not None:
        return await _create(conn)

    pool = get_pool()
    async with pool.acquire() as active_conn:
        return await _create(active_conn)


async def list_todo_items(
    *,
    session_id: str,
    execution_id: Optional[str] = None,
    message_id: Optional[str] = None,
    statuses: Optional[Iterable[str]] = None,
    include_completed: bool = True,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    sid = uuid.UUID(str(session_id))
    params: list[Any] = [sid]
    conditions = ["session_id = $1"]
    index = 2
    if execution_id:
        params.append(uuid.UUID(str(execution_id)))
        conditions.append(f"execution_id = ${index}")
        index += 1
    if message_id:
        params.append(uuid.UUID(str(message_id)))
        conditions.append(f"message_id = ${index}")
        index += 1
    if statuses:
        params.append(list(statuses))
        conditions.append(f"status = ANY(${index}::text[])")
        index += 1
    elif not include_completed:
        params.append(list(TODO_ACTIVE_STATUSES))
        conditions.append(f"status = ANY(${index}::text[])")
        index += 1

    query = f"""
        SELECT *
        FROM chat_todo_items
        WHERE {' AND '.join(conditions)}
        ORDER BY
            CASE WHEN status IN ('pending', 'in_progress') THEN 0 ELSE 1 END,
            sort_order ASC,
            created_at ASC
    """

    async def _list(active_conn: Any) -> list[dict[str, Any]]:
        rows = await active_conn.fetch(query, *params)
        return [_normalize_todo_row(row) for row in rows]

    if conn is not None:
        return await _list(conn)

    pool = get_pool()
    async with pool.acquire() as active_conn:
        return await _list(active_conn)


async def cleanup_stale_in_progress_todos(
    *,
    session_id: str,
    stale_after_minutes: int = DEFAULT_STALE_IN_PROGRESS_MINUTES,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    """Reset old in_progress rows so stale work does not block the next turn."""
    sid = uuid.UUID(str(session_id))
    stale_after_minutes = max(5, int(stale_after_minutes or DEFAULT_STALE_IN_PROGRESS_MINUTES))

    async def _cleanup(active_conn: Any) -> list[dict[str, Any]]:
        rows = await active_conn.fetch(
            """
            UPDATE chat_todo_items
            SET status = $3,
                metadata = jsonb_set(
                    jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{stale_reset_at}',
                        to_jsonb(NOW()::text),
                        true
                    ),
                    '{stale_reset_reason}',
                    to_jsonb('in_progress_timeout'::text),
                    true
                ),
                updated_at = NOW(),
                completed_at = NULL
            WHERE session_id = $1
              AND status = $2
              AND updated_at < NOW() - make_interval(mins => $4::int)
            RETURNING *
            """,
            sid,
            TODO_STATUS_IN_PROGRESS,
            TODO_STATUS_PENDING,
            stale_after_minutes,
        )
        reset_rows = [_normalize_todo_row(row) for row in rows]
        if reset_rows:
            logger.info(
                "chat_todo_stale_reset session=%s count=%s minutes=%s",
                str(session_id)[:8],
                len(reset_rows),
                stale_after_minutes,
            )
        active_rows = await active_conn.fetch(
            """
            SELECT *
            FROM chat_todo_items
            WHERE session_id = $1
              AND status = ANY($2::text[])
            ORDER BY sort_order ASC, created_at ASC
            """,
            sid,
            list(TODO_ACTIVE_STATUSES),
        )
        has_in_progress = any(row["status"] == TODO_STATUS_IN_PROGRESS for row in active_rows)
        if not has_in_progress and active_rows:
            promoted = await update_todo_item(
                todo_id=str(active_rows[0]["id"]),
                status=TODO_STATUS_IN_PROGRESS,
                metadata={
                    "stale_cleanup_promoted": True,
                    "stale_after_minutes": stale_after_minutes,
                },
                source="stale_cleanup",
                conn=active_conn,
            )
            if promoted:
                reset_rows.append(promoted)
        return reset_rows

    if conn is not None:
        return await _cleanup(conn)

    pool = get_pool()
    async with pool.acquire() as active_conn:
        return await _cleanup(active_conn)


async def update_todo_item(
    *,
    todo_id: str,
    status: Optional[str] = None,
    title: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    source: str = "system",
    conn: Any | None = None,
) -> Optional[dict[str, Any]]:
    if status and status not in TODO_VALID_STATUSES:
        raise ValueError(f"invalid todo status: {status}")
    todo_uuid = uuid.UUID(str(todo_id))

    async def _update(active_conn: Any) -> Optional[dict[str, Any]]:
        current = await active_conn.fetchrow(
            "SELECT * FROM chat_todo_items WHERE id = $1",
            todo_uuid,
        )
        if not current:
            return None
        current_dict = dict(current)
        merged_metadata = _normalize_metadata(current_dict.get("metadata"))
        merged_metadata.update(_normalize_metadata(metadata))
        merged_metadata = _append_audit(
            merged_metadata,
            action="update",
            source=source,
            detail={"status": status or current_dict.get("status", "")},
        )
        next_status = status or current_dict["status"]
        completed_at = current_dict.get("completed_at")
        if next_status in TODO_TERMINAL_STATUSES:
            completed_at = completed_at or _utcnow()
        elif next_status in TODO_ACTIVE_STATUSES:
            completed_at = None
        row = await active_conn.fetchrow(
            """
            UPDATE chat_todo_items
            SET title = $2,
                status = $3,
                metadata = $4::jsonb,
                updated_at = NOW(),
                completed_at = $5
            WHERE id = $1
            RETURNING *
            """,
            todo_uuid,
            title or current_dict["title"],
            next_status,
            json.dumps(merged_metadata, ensure_ascii=False),
            completed_at,
        )
        return _normalize_todo_row(row) if row else None

    if conn is not None:
        return await _update(conn)

    pool = get_pool()
    async with pool.acquire() as active_conn:
        return await _update(active_conn)


async def mark_complete(
    todo_id: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
    source: str = "completion_gate",
    conn: Any | None = None,
) -> Optional[dict[str, Any]]:
    return await update_todo_item(
        todo_id=todo_id,
        status=TODO_STATUS_COMPLETED,
        metadata=metadata,
        source=source,
        conn=conn,
    )


async def mark_failed(
    todo_id: str,
    *,
    reason: str = "",
    metadata: Optional[dict[str, Any]] = None,
    source: str = "completion_gate",
    conn: Any | None = None,
) -> Optional[dict[str, Any]]:
    merged = _normalize_metadata(metadata)
    if reason:
        merged["failure_reason"] = reason
    return await update_todo_item(
        todo_id=todo_id,
        status=TODO_STATUS_FAILED,
        metadata=merged,
        source=source,
        conn=conn,
    )
