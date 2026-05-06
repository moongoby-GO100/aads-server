"""
F5: Tool Result Archive — 도구 실행 결과 전문을 tool_results_archive에 보관.
재실행 없이 과거 결과 즉시 참조 가능.
비용: 0 (DB 쓰기만).
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

_ARCHIVE_SCHEMA_CACHE: Optional[dict[str, str]] = None
_HIGH_COST_SUMMARY_TOOLS = frozenset({
    "run_agent_team",
    "deep_research",
})
_DEFAULT_RESULT_SUMMARY_LIMIT = 1000
_HIGH_COST_RESULT_SUMMARY_LIMIT = 500
_INPUT_SUMMARY_LIMIT = 1000
_ERROR_DETAIL_LIMIT = 2000
_RAW_OUTPUT_LIMIT = 500000
_TRUNCATED_SUFFIX = "...[truncated]"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, uuid.UUID):
        return str(obj)
    return str(obj)


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATED_SUFFIX):
        return text[:limit]
    return text[: limit - len(_TRUNCATED_SUFFIX)] + _TRUNCATED_SUFFIX


def _squash_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _compact_json_value(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return _truncate_text(_squash_whitespace(str(value)), 300)
    if isinstance(value, dict):
        items = list(value.items())[:30]
        compacted = {str(k): _compact_json_value(v, depth + 1) for k, v in items}
        if len(value) > len(items):
            compacted["_truncated_keys"] = len(value) - len(items)
        return compacted
    if isinstance(value, list):
        items = value[:20]
        compacted = [_compact_json_value(v, depth + 1) for v in items]
        if len(value) > len(items):
            compacted.append(f"...[{len(value) - len(items)} more items]")
        return compacted
    if isinstance(value, tuple):
        return _compact_json_value(list(value), depth)
    if isinstance(value, str):
        return _truncate_text(_squash_whitespace(value), 300)
    return value


def _summarize_input_params(input_params: Any) -> tuple[str, Any]:
    compacted = _compact_json_value(input_params if input_params is not None else {})
    try:
        serialized = json.dumps(compacted, ensure_ascii=False, sort_keys=True, default=_json_default)
    except Exception:
        serialized = _truncate_text(_squash_whitespace(str(input_params)), _INPUT_SUMMARY_LIMIT)
        compacted = {"_summary": serialized}
    summary = _truncate_text(serialized, _INPUT_SUMMARY_LIMIT)
    if len(serialized) > _INPUT_SUMMARY_LIMIT:
        compacted = {"_summary": summary}
    return summary, compacted


def _detect_error(raw_output: str) -> tuple[bool, Optional[str]]:
    text = str(raw_output or "")
    parsed: Any = None
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except Exception:
            parsed = None

    if isinstance(parsed, dict):
        error_value = parsed.get("error") or parsed.get("error_detail")
        if error_value:
            return True, _truncate_text(_squash_whitespace(str(error_value)), _ERROR_DETAIL_LIMIT)
        if parsed.get("success") is False:
            detail = parsed.get("message") or parsed.get("status") or text
            return True, _truncate_text(_squash_whitespace(str(detail)), _ERROR_DETAIL_LIMIT)
        status = str(parsed.get("status") or "").lower().strip()
        if status in {"error", "failed", "failure", "timeout"}:
            detail = parsed.get("message") or parsed.get("status") or text
            return True, _truncate_text(_squash_whitespace(str(detail)), _ERROR_DETAIL_LIMIT)

    text_sample = text[:2000]
    lower = text_sample.lower()
    is_error = (
        "[error]" in lower
        or '"error"' in lower
        or "exception" in lower
        or "traceback" in lower
        or "timeout" in lower
        or "old_string을 찾을 수 없음" in text_sample
        or "exit=1" in text_sample[:200]
        or "exit=137" in text_sample[:200]
        or "허용되지 않은" in text_sample[:200]
        or "차단" in text_sample[:200]
        or "permissionerror" in lower
        or "no such file" in lower
        or "unknown_tool" in lower
    )
    if not is_error:
        return False, None
    return True, _truncate_text(_squash_whitespace(text_sample), _ERROR_DETAIL_LIMIT)


def _summarize_result(tool_name: str, raw_output: str, error_detail: Optional[str]) -> str:
    base = error_detail or raw_output or ""
    stripped = base.strip()
    if stripped.startswith(("{", "[")):
        try:
            base = json.dumps(json.loads(stripped), ensure_ascii=False, sort_keys=True, default=_json_default)
        except Exception:
            base = stripped
    limit = (
        _HIGH_COST_RESULT_SUMMARY_LIMIT
        if tool_name in _HIGH_COST_SUMMARY_TOOLS
        else _DEFAULT_RESULT_SUMMARY_LIMIT
    )
    return _truncate_text(_squash_whitespace(base), limit)


async def _get_archive_schema(conn) -> dict[str, str]:
    global _ARCHIVE_SCHEMA_CACHE
    if _ARCHIVE_SCHEMA_CACHE is not None:
        return _ARCHIVE_SCHEMA_CACHE

    rows = await conn.fetch(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'tool_results_archive'
        ORDER BY ordinal_position
        """
    )
    _ARCHIVE_SCHEMA_CACHE = {
        str(r["column_name"]): str(r["data_type"]).lower()
        for r in rows
    }
    return _ARCHIVE_SCHEMA_CACHE


def _placeholder_for_type(index: int, data_type: str) -> str:
    if data_type == "jsonb":
        return f"${index}::jsonb"
    if data_type == "json":
        return f"${index}::json"
    if data_type == "uuid":
        return f"${index}::uuid"
    return f"${index}"


async def _insert_archive_row(
    conn,
    *,
    message_id: Optional[str],
    tool_use_id: Optional[str],
    tool_name: str,
    input_params: Any,
    raw_output: str,
    latency_ms: int = 0,
    success: Optional[bool] = None,
    error_detail: Optional[str] = None,
) -> bool:
    if not tool_name:
        return False

    schema = await _get_archive_schema(conn)
    if not schema:
        return False

    from app.core.token_utils import estimate_tokens

    input_summary, input_payload = _summarize_input_params(input_params)
    detected_error, detected_detail = _detect_error(raw_output)
    final_success = (not detected_error) if success is None else bool(success)
    final_error_detail = error_detail
    if not final_success:
        final_error_detail = final_error_detail or detected_detail or "tool execution failed"
    final_error_detail = _truncate_text(_squash_whitespace(str(final_error_detail or "")), _ERROR_DETAIL_LIMIT)
    if final_success and not final_error_detail:
        final_error_detail = ""

    result_summary = _summarize_result(tool_name, raw_output, final_error_detail or None)
    output_tokens = estimate_tokens(raw_output or result_summary)

    values: list[Any] = []
    columns: list[str] = []
    placeholders: list[str] = []

    def add_column(column_name: str, value: Any) -> None:
        data_type = schema.get(column_name)
        if not data_type:
            return
        columns.append(column_name)
        values.append(value)
        placeholders.append(_placeholder_for_type(len(values), data_type))

    if "message_id" in schema:
        if not message_id:
            return False
        add_column("message_id", str(uuid.UUID(str(message_id))))

    if "tool_use_id" in schema:
        add_column("tool_use_id", str(tool_use_id or uuid.uuid4()))

    add_column("tool_name", tool_name)

    if "input_params" in schema:
        input_type = schema["input_params"]
        if input_type in {"json", "jsonb"}:
            add_column("input_params", json.dumps(input_payload, ensure_ascii=False, default=_json_default))
        else:
            add_column("input_params", input_summary)

    if "raw_output" in schema:
        add_column("raw_output", _truncate_text(raw_output or "", _RAW_OUTPUT_LIMIT))

    if "output_tokens" in schema:
        add_column("output_tokens", int(output_tokens))

    if "is_error" in schema:
        add_column("is_error", not final_success)

    if "result_summary" in schema:
        add_column("result_summary", result_summary)

    if "latency_ms" in schema:
        add_column("latency_ms", int(latency_ms))

    if "success" in schema:
        add_column("success", final_success)

    if "error_detail" in schema:
        add_column("error_detail", final_error_detail or None)

    if not columns:
        return False

    query = (
        "INSERT INTO tool_results_archive "
        f"({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
    )

    if "message_id" in schema and "tool_use_id" in schema:
        updatable_cols = [
            col for col in columns
            if col not in {"message_id", "tool_use_id", "created_at"}
        ]
        if updatable_cols:
            query += (
                " ON CONFLICT (message_id, tool_use_id) DO UPDATE SET "
                + ", ".join(f"{col} = EXCLUDED.{col}" for col in updatable_cols)
            )
        else:
            query += " ON CONFLICT (message_id, tool_use_id) DO NOTHING"

    await conn.execute(query, *values)
    return True


async def archive_tool_execution(
    session_id: str,
    tool_name: str,
    input_params: Any,
    raw_output: str,
    latency_ms: int = 0,
    tool_use_id: Optional[str] = None,
) -> bool:
    """채팅 세션 기준으로 가장 최근 사용자 메시지에 도구 실행 결과를 저장."""
    if not session_id or not tool_name:
        return False

    try:
        uuid.UUID(str(session_id))
    except Exception:
        return False

    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            message_id = await conn.fetchval(
                """
                SELECT id::text
                FROM chat_messages
                WHERE session_id = $1::uuid AND role = 'user'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                session_id,
            )
            if not message_id:
                return False
            return await _insert_archive_row(
                conn,
                message_id=message_id,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                input_params=input_params,
                raw_output=raw_output,
                latency_ms=latency_ms,
            )
    except Exception as e:
        logger.warning("tool_archive_execution_error", error=str(e), tool=tool_name)
        return False


async def archive_tool_result(
    message_id: str,
    tool_use_id: str,
    tool_name: str,
    input_params: Any,
    raw_output: str,
    latency_ms: int = 0,
    success: Optional[bool] = None,
    error_detail: Optional[str] = None,
) -> bool:
    """도구 실행 결과를 tool_results_archive에 저장."""
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            return await _insert_archive_row(
                conn,
                message_id=message_id,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                input_params=input_params,
                raw_output=raw_output,
                latency_ms=latency_ms,
                success=success,
                error_detail=error_detail,
            )
    except Exception as e:
        logger.warning("tool_archive_save_error", error=str(e), tool=tool_name)
        return False


async def get_tool_error_stats(hours: int = 24) -> list:
    """최근 N시간 도구별 성공/실패 횟수, 오류율, 마지막 오류 메시지 반환."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            schema = await _get_archive_schema(conn)
            error_expr = (
                "CASE WHEN is_error THEN 1 ELSE 0 END"
                if "is_error" in schema
                else "CASE WHEN success IS FALSE THEN 1 ELSE 0 END"
                if "success" in schema
                else "0"
            )
            error_filter = (
                "t2.is_error = TRUE"
                if "is_error" in schema
                else "t2.success = FALSE"
                if "success" in schema
                else "FALSE"
            )
            last_error_parts = []
            if "error_detail" in schema:
                last_error_parts.append("t2.error_detail")
            if "result_summary" in schema:
                last_error_parts.append("t2.result_summary")
            if "raw_output" in schema:
                last_error_parts.append("t2.raw_output")
            last_error_col = (
                f"COALESCE({', '.join(last_error_parts)})"
                if len(last_error_parts) > 1
                else last_error_parts[0]
                if last_error_parts
                else "NULL"
            )
            rows = await conn.fetch(
                f"""
                SELECT
                    tool_name,
                    COUNT(*) AS total,
                    SUM({error_expr}) AS errors,
                    (
                        SELECT {last_error_col} FROM tool_results_archive t2
                        WHERE t2.tool_name = t.tool_name AND {error_filter}
                        ORDER BY t2.created_at DESC LIMIT 1
                    ) AS last_error
                FROM tool_results_archive t
                WHERE created_at > NOW() - ($1 * INTERVAL '1 hour')
                GROUP BY tool_name
                ORDER BY errors DESC, total DESC
                """,
                hours,
            )
        return [
            {
                "tool_name": r["tool_name"],
                "total": int(r["total"]),
                "errors": int(r["errors"]),
                "error_rate": round(float(r["errors"]) / float(r["total"]), 4) if r["total"] else 0.0,
                "last_error": r["last_error"][:500] if r["last_error"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("tool_stats_error", error=str(e))
        return []


async def recall_tool_result(
    tool_name: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 5,
) -> list:
    """과거 도구 실행 결과를 검색."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        async with pool.acquire() as conn:
            schema = await _get_archive_schema(conn)
            output_col = (
                "raw_output"
                if "raw_output" in schema
                else "result_summary"
                if "result_summary" in schema
                else ""
            )
            if not output_col:
                return []

            input_col = "input_params" if "input_params" in schema else "NULL"
            tool_use_col = "tool_use_id" if "tool_use_id" in schema else "NULL"
            select_sql = (
                f"SELECT tool_name, {tool_use_col} AS tool_use_id, {input_col} AS input_params, "
                f"{output_col} AS output_text, created_at "
                f"FROM tool_results_archive"
            )
            if tool_name and keyword:
                rows = await conn.fetch(
                    f"""
                    {select_sql}
                    WHERE tool_name = $1 AND {output_col} ILIKE $2
                    ORDER BY created_at DESC LIMIT $3
                    """,
                    tool_name, f"%{keyword}%", limit,
                )
            elif tool_name:
                rows = await conn.fetch(
                    f"""
                    {select_sql}
                    WHERE tool_name = $1
                    ORDER BY created_at DESC LIMIT $2
                    """,
                    tool_name, limit,
                )
            elif keyword:
                rows = await conn.fetch(
                    f"""
                    {select_sql}
                    WHERE {output_col} ILIKE $1
                    ORDER BY created_at DESC LIMIT $2
                    """,
                    f"%{keyword}%", limit,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    {select_sql}
                    ORDER BY created_at DESC LIMIT $1
                    """,
                    limit,
                )

            return [
                {
                    "tool_name": r["tool_name"],
                    "input_params": r["input_params"],
                    "output_preview": (r["output_text"] or "")[:1000],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning("tool_archive_recall_error", error=str(e))
        return []
