"""
F5: Tool Result Archive — 도구 실행 결과 전문을 tool_results_archive에 보관.
재실행 없이 과거 결과 즉시 참조 가능.
비용: 0 (DB 쓰기만).
"""
from __future__ import annotations

import uuid
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


async def archive_tool_result(
    message_id: str,
    tool_use_id: str,
    tool_name: str,
    input_params: dict,
    raw_output: str,
) -> bool:
    """도구 실행 결과를 tool_results_archive에 저장."""
    if not raw_output or not tool_name:
        return False

    try:
        import json
        from app.core.db_pool import get_pool
        from app.core.token_utils import estimate_tokens

        pool = get_pool()
        output_tokens = estimate_tokens(raw_output)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tool_results_archive
                    (message_id, tool_use_id, tool_name, input_params, raw_output, output_tokens)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                ON CONFLICT (message_id, tool_use_id) DO UPDATE SET
                    raw_output = EXCLUDED.raw_output,
                    output_tokens = EXCLUDED.output_tokens
                """,
                uuid.UUID(message_id),
                tool_use_id,
                tool_name,
                json.dumps(input_params or {}),
                raw_output[:500000],  # 500KB limit
                output_tokens,
            )
        return True
    except Exception as e:
        logger.debug("tool_archive_save_error", error=str(e), tool=tool_name)
        return False


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
            if tool_name and keyword:
                rows = await conn.fetch(
                    """
                    SELECT tool_name, tool_use_id, input_params, raw_output, created_at
                    FROM tool_results_archive
                    WHERE tool_name = $1 AND raw_output ILIKE $2
                    ORDER BY created_at DESC LIMIT $3
                    """,
                    tool_name, f"%{keyword}%", limit,
                )
            elif tool_name:
                rows = await conn.fetch(
                    """
                    SELECT tool_name, tool_use_id, input_params, raw_output, created_at
                    FROM tool_results_archive
                    WHERE tool_name = $1
                    ORDER BY created_at DESC LIMIT $2
                    """,
                    tool_name, limit,
                )
            elif keyword:
                rows = await conn.fetch(
                    """
                    SELECT tool_name, tool_use_id, input_params, raw_output, created_at
                    FROM tool_results_archive
                    WHERE raw_output ILIKE $1
                    ORDER BY created_at DESC LIMIT $2
                    """,
                    f"%{keyword}%", limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT tool_name, tool_use_id, input_params, raw_output, created_at
                    FROM tool_results_archive
                    ORDER BY created_at DESC LIMIT $1
                    """,
                    limit,
                )

            return [
                {
                    "tool_name": r["tool_name"],
                    "input_params": r["input_params"],
                    "output_preview": r["raw_output"][:1000],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning("tool_archive_recall_error", error=str(e))
        return []
