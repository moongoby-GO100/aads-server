"""ohvis_harness_traces 비치명적 기록 헬퍼.

`migrations/158_ohvis_harness_skill_wiki_foundation.sql`이 만든
`ohvis_harness_traces` 테이블에 실행 근거를 남긴다.

설계 원칙:
- **절대 예외를 밖으로 던지지 않는다.** 호출부 동작은 trace 실패와 무관해야 한다.
- 테이블이 없으면 경고 1회만 남기고 이후 조용히 skip한다 (마이그레이션 미적용 환경).
- 요약 문자열은 잘라서 저장한다 (trace가 본문 저장소가 되면 안 된다).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

TRACE_TABLE = "ohvis_harness_traces"
SUMMARY_LIMIT = 500
ERROR_LIMIT = 1000

_INSERT_SQL = f"""
INSERT INTO {TRACE_TABLE} (
    graph_run_id, project, session_id, ohvis_task_id, provider,
    trace_id, span_id, run_type, input_summary, output_summary,
    tool_calls, latency_ms, error, metadata
)
VALUES (
    $1, $2, $3::uuid, $4::uuid, $5,
    $6, $7, $8, $9, $10,
    $11::jsonb, $12, $13, $14::jsonb
)
"""

# None=미확인, True=존재, False=없음(경고 1회 후 skip)
_table_present: Optional[bool] = None


def reset_table_cache() -> None:
    """테이블 존재 여부 캐시를 초기화한다 (테스트/마이그레이션 직후용)."""
    global _table_present
    _table_present = None


def _clip(value: Any, limit: int = SUMMARY_LIMIT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _as_uuid_text(value: Any) -> Optional[str]:
    """UUID로 해석 가능하면 문자열로, 아니면 None (::uuid 캐스팅 실패 방지)."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _as_json(value: Any, default: str) -> str:
    if value is None:
        return default
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return default


async def _table_exists(conn: Any) -> bool:
    global _table_present
    if _table_present is not None:
        return _table_present
    present = bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='public' AND table_name=$1
            )
            """,
            TRACE_TABLE,
        )
    )
    _table_present = present
    if not present:
        logger.warning(
            "%s table is missing; harness traces are skipped until migration 158 is applied",
            TRACE_TABLE,
        )
    return present


async def record_trace(
    *,
    graph_run_id: str,
    project: Optional[str] = None,
    run_type: str = "chain",
    input_summary: Any = "",
    output_summary: Any = "",
    metadata: Optional[dict[str, Any]] = None,
    tool_calls: Optional[list[Any]] = None,
    session_id: Optional[str] = None,
    ohvis_task_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    provider: str = "internal",
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> bool:
    """trace 1건을 기록한다. 성공하면 True, 실패/skip이면 False (예외 없음)."""
    if not graph_run_id:
        return False
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            if not await _table_exists(conn):
                return False
            await conn.execute(
                _INSERT_SQL,
                str(graph_run_id)[:200],
                (project or None),
                _as_uuid_text(session_id),
                _as_uuid_text(ohvis_task_id),
                provider or "internal",
                trace_id,
                span_id,
                run_type or "chain",
                _clip(input_summary),
                _clip(output_summary),
                _as_json(tool_calls, "[]"),
                int(latency_ms) if latency_ms is not None else None,
                _clip(error, ERROR_LIMIT) if error else None,
                _as_json(metadata, "{}"),
            )
        return True
    except Exception as exc:  # noqa: BLE001 — trace는 절대 호출부를 깨뜨리지 않는다
        logger.warning("harness trace insert failed (non-fatal): %s", str(exc)[:200])
        return False


async def record_goal_trace(
    action: str,
    *,
    goal_id: Optional[str] = None,
    project: Optional[str] = None,
    milestone_id: Optional[str] = None,
    task_ref: Optional[str] = None,
    outcome: Any = None,
    detail: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> bool:
    """Goal Control Loop 전용 편의 래퍼 — 간결한 trace 1건을 남긴다."""
    metadata: dict[str, Any] = {"component": "goal_control_loop", "action": action}
    if goal_id:
        metadata["goal_id"] = str(goal_id)
    if milestone_id:
        metadata["milestone_id"] = str(milestone_id)
    if task_ref:
        metadata["task_ref"] = str(task_ref)
    if detail:
        metadata["detail"] = detail

    parts = [f"goal.{action}"]
    if project:
        parts.append(f"project={project}")
    if goal_id:
        parts.append(f"goal_id={goal_id}")
    if milestone_id:
        parts.append(f"milestone_id={milestone_id}")
    if task_ref:
        parts.append(f"task={task_ref}")

    return await record_trace(
        graph_run_id=f"goal:{goal_id}" if goal_id else f"goal:{action}",
        project=project,
        run_type="chain",
        input_summary=" ".join(parts),
        output_summary=_clip(outcome) if outcome is not None else "",
        metadata=metadata,
        error=error,
    )
