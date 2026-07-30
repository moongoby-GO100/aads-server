"""OHVIS Loop Controller — Phase 0 핵심 모듈.

루프 생명주기 관리 (생성/시작/정지/삭제) + 모델별 비용 자동 조정.
기획서: docs/AADS-LAYOUT-001_OHVIS-LOOP-SYSTEM.md §6.3, §8
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from app.core.db_pool import get_pool as get_db_pool

logger = logging.getLogger("ohvis.loop_controller")

# --- 비용 자동 조정 상수 (§6.3) ---
_BASE_BUDGET: dict[str, float] = {
    "monitor": 0.50,
    "task": 3.00,
    "sequential": 6.00,
}
_MIN_BUDGET = 0.50
_MAX_BUDGET_CEO_OVERRIDE = 30.00
_SONNET_BLENDED = 18.0  # Sonnet input(3) + output(15)

# --- 루프 유형별 기본 제한 (§6.1) ---
_DEFAULT_LIMITS: dict[str, dict[str, Any]] = {
    "monitor": {
        "max_iterations": 100,
        "max_failures": 5,
        "timeout_minutes": 2880,
        "min_interval": 60,
        "default_interval": 1800,
        "llm_per_iteration": 3,
    },
    "task": {
        "max_iterations": 10,
        "max_failures": 3,
        "timeout_minutes": 240,
        "min_interval": 30,
        "default_interval": None,
        "llm_per_iteration": 15,
    },
    "sequential": {
        "max_iterations": None,  # task_count × 3
        "max_failures": 3,
        "timeout_minutes": 480,
        "min_interval": 10,
        "default_interval": None,
        "llm_per_iteration": 15,
    },
}

VALID_LOOP_TYPES = {"monitor", "task", "sequential"}
VALID_STATUSES = {"active", "paused", "completed", "failed", "cancelled"}


async def resolve_max_cost(
    loop_type: str,
    model_id: str | None,
    ceo_override: float | None = None,
) -> float:
    """모델 단가 기반 비용 상한 자동 산출 (§6.3.3).

    CEO가 수동 지정하면 그 값 사용 (최대 $30).
    그 외: DB llm_models에서 input_cost + output_cost를 조회해 배율 계산.
    """
    if ceo_override is not None:
        return min(float(ceo_override), _MAX_BUDGET_CEO_OVERRIDE)

    base = _BASE_BUDGET.get(loop_type, _BASE_BUDGET["task"])

    if not model_id:
        return base

    pool = get_db_pool()
    row = await pool.fetchrow(
        "SELECT input_cost, output_cost FROM llm_models "
        "WHERE model_id = $1 AND is_active = true",
        model_id,
    )
    if not row or row["input_cost"] is None or row["output_cost"] is None:
        return base

    blended = float(row["input_cost"]) + float(row["output_cost"])
    multiplier = blended / _SONNET_BLENDED if _SONNET_BLENDED > 0 else 1.0
    return max(round(base * multiplier, 2), _MIN_BUDGET)


async def create_loop(
    loop_type: str,
    original_command: str,
    project: str = "AADS",
    *,
    parsed_intent: dict | None = None,  # DB NOT NULL — {} 기본
    interval_seconds: int | None = None,
    max_iterations: int | None = None,
    max_cost_override: float | None = None,
    max_failures: int | None = None,
    timeout_minutes: int | None = None,
    success_condition: dict | None = None,
    alert_condition: dict | None = None,
    execution_model_id: str | None = None,
    session_id: str | None = None,
    task_list: list | None = None,
) -> dict:
    """루프 생성 + DB INSERT + 비용 자동 산출."""
    if loop_type not in VALID_LOOP_TYPES:
        raise ValueError(f"Invalid loop_type: {loop_type}")

    defaults = _DEFAULT_LIMITS[loop_type]

    if interval_seconds is not None and defaults["min_interval"] is not None:
        interval_seconds = max(interval_seconds, defaults["min_interval"])
    elif interval_seconds is None:
        interval_seconds = defaults["default_interval"]

    if max_iterations is None:
        if loop_type == "sequential" and task_list:
            max_iterations = len(task_list) * 3
        else:
            max_iterations = defaults["max_iterations"]

    if max_failures is None:
        max_failures = defaults["max_failures"]
    if timeout_minutes is None:
        timeout_minutes = defaults["timeout_minutes"]

    cost_override_by_ceo = max_cost_override is not None
    max_cost_usd = await resolve_max_cost(
        loop_type, execution_model_id, max_cost_override
    )

    next_run_at = None
    if interval_seconds is not None:
        next_run_at = datetime.now() + timedelta(seconds=interval_seconds)

    pool = get_db_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO ohvis_loops (
            loop_type, status, original_command, parsed_intent,
            interval_seconds, max_iterations, max_cost_usd,
            execution_model_id, cost_override_by_ceo,
            max_failures, timeout_minutes,
            success_condition, alert_condition,
            project, session_id, started_at, next_run_at
        ) VALUES (
            $1, 'active', $2, $3::jsonb,
            $4, $5, $6,
            $7, $8,
            $9, $10,
            $11::jsonb, $12::jsonb,
            $13, $14, NOW(), $15
        )
        RETURNING id, loop_type, status, max_cost_usd, max_iterations,
                  interval_seconds, execution_model_id, project
        """,
        loop_type,
        original_command,
        _json_or_null(parsed_intent or {}),
        interval_seconds,
        max_iterations,
        max_cost_usd,
        execution_model_id,
        cost_override_by_ceo,
        max_failures,
        timeout_minutes,
        _json_or_null(success_condition),
        _json_or_null(alert_condition),
        project,
        session_id,
        next_run_at,
    )

    result = dict(row)
    logger.info(
        "Loop #%d created: type=%s project=%s model=%s budget=$%.2f",
        result["id"], loop_type, project, execution_model_id, max_cost_usd,
    )
    return result


async def get_loop(loop_id: int) -> dict | None:
    pool = get_db_pool()
    row = await pool.fetchrow("SELECT * FROM ohvis_loops WHERE id = $1", loop_id)
    return dict(row) if row else None


async def list_active_loops(
    project: str | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict]:
    """루프 목록 조회.

    status='active'(기본) | 'all'(전체 이력) | 'completed'/'failed'/'paused'/'cancelled'.
    대시보드에서 완료·실패 이력까지 확인할 수 있도록 status 필터를 지원한다.
    (AADS-LOOP P1, 2026-07-30)
    """
    pool = get_db_pool()
    limit = max(1, min(int(limit or 50), 200))
    conds: list[str] = []
    args: list = []
    if status and status != "all":
        args.append(status)
        conds.append(f"status = ${len(args)}")
    if project:
        args.append(project)
        conds.append(f"project = ${len(args)}")
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    rows = await pool.fetch(
        f"SELECT * FROM ohvis_loops {where} ORDER BY created_at DESC LIMIT {limit}",
        *args,
    )
    return [dict(r) for r in rows]


async def update_loop_status(loop_id: int, new_status: str, reason: str = "") -> dict | None:
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {new_status}")

    pool = get_db_pool()
    completed_at = "NOW()" if new_status in ("completed", "failed", "cancelled") else "NULL"
    row = await pool.fetchrow(
        f"""
        UPDATE ohvis_loops
        SET status = $1,
            last_result = COALESCE(last_result, '{{}}'::jsonb) || jsonb_build_object('status_reason', $2::text),
            completed_at = {completed_at}
        WHERE id = $3
        RETURNING id, status, total_cost_usd, current_iteration
        """,
        new_status, reason, loop_id,
    )
    if row:
        logger.info("Loop #%d → %s (%s)", loop_id, new_status, reason)
    return dict(row) if row else None


async def pause_loop(loop_id: int, reason: str = "CEO 요청") -> dict | None:
    return await update_loop_status(loop_id, "paused", reason)


async def resume_loop(loop_id: int) -> dict | None:
    pool = get_db_pool()
    row = await pool.fetchrow(
        """
        UPDATE ohvis_loops
        SET status = 'active',
            next_run_at = NOW(),
            last_result = COALESCE(last_result, '{}'::jsonb) || '{"status_reason":"resumed"}'::jsonb
        WHERE id = $1 AND status = 'paused'
        RETURNING id, status
        """,
        loop_id,
    )
    if row:
        logger.info("Loop #%d resumed", loop_id)
    return dict(row) if row else None


async def cancel_loop(loop_id: int, reason: str = "CEO 중단 명령") -> dict | None:
    return await update_loop_status(loop_id, "cancelled", reason)


async def record_iteration(
    loop_id: int,
    iteration_num: int,
    status: str,
    *,
    result_summary: str = "",
    result_data: dict | None = None,
    llm_calls: int = 0,
    cost_usd: float = 0.0,
    duration_ms: int = 0,
    model_used: str | None = None,
    alert_sent: bool = False,
    alert_channel: str | None = None,
    ohvis_task_id: int | None = None,
) -> dict:
    """iteration 기록 + 루프 누적 비용/카운터 갱신."""
    pool = get_db_pool()

    iter_row = await pool.fetchrow(
        """
        INSERT INTO ohvis_loop_iterations (
            loop_id, iteration_num, status, result_summary, result_data,
            llm_calls, cost_usd, duration_ms, model_used,
            alert_sent, alert_channel, ohvis_task_id
        ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id
        """,
        loop_id, iteration_num, status, result_summary,
        _json_or_null(result_data),
        llm_calls, cost_usd, duration_ms, model_used,
        alert_sent, alert_channel, ohvis_task_id,
    )

    await pool.execute(
        """
        UPDATE ohvis_loops
        SET current_iteration = $1,
            total_cost_usd = total_cost_usd + $2,
            consecutive_failures = CASE
                WHEN $3 = 'failure' THEN consecutive_failures + 1
                ELSE 0
            END,
            last_result = $4::jsonb,
            next_run_at = CASE
                WHEN interval_seconds IS NOT NULL
                THEN NOW() + make_interval(secs := interval_seconds)
                ELSE NULL
            END
        WHERE id = $5
        """,
        iteration_num, Decimal(str(cost_usd)),
        status, _json_or_null(result_data or {"summary": result_summary}),
        loop_id,
    )

    return {"iteration_id": iter_row["id"], "loop_id": loop_id}


async def check_safety_limits(loop_id: int) -> dict:
    """안전 제한 체크. 초과 시 pause/fail 사유 반환."""
    loop = await get_loop(loop_id)
    if not loop:
        return {"ok": False, "reason": "loop_not_found"}

    if loop["status"] != "active":
        return {"ok": False, "reason": f"loop_not_active ({loop['status']})"}

    if loop["current_iteration"] >= loop["max_iterations"]:
        return {"ok": False, "reason": "max_iterations_reached", "action": "complete"}

    if float(loop["total_cost_usd"]) >= float(loop["max_cost_usd"]):
        return {"ok": False, "reason": "cost_limit_reached", "action": "pause"}

    if loop["timeout_minutes"] and loop["started_at"]:
        elapsed = (datetime.utcnow() - loop["started_at"]).total_seconds() / 60
        if elapsed >= loop["timeout_minutes"]:
            return {"ok": False, "reason": "timeout", "action": "pause"}

    if loop["consecutive_failures"] >= loop["max_failures"]:
        return {"ok": False, "reason": "max_failures_reached", "action": "fail"}

    cost_pct = (
        float(loop["total_cost_usd"]) / float(loop["max_cost_usd"]) * 100
        if float(loop["max_cost_usd"]) > 0 else 0
    )
    return {"ok": True, "cost_pct": round(cost_pct, 1)}


async def recalculate_cost_on_fallback(loop_id: int, new_model_id: str) -> float:
    """폴백으로 모델 변경 시 max_cost_usd 재산출 (§6.3 모델 변경 시 재산출)."""
    loop = await get_loop(loop_id)
    if not loop or loop.get("cost_override_by_ceo"):
        return float(loop["max_cost_usd"]) if loop else 0.0

    new_max = await resolve_max_cost(loop["loop_type"], new_model_id)

    pool = get_db_pool()
    await pool.execute(
        "UPDATE ohvis_loops SET max_cost_usd = $1, execution_model_id = $2 WHERE id = $3",
        Decimal(str(new_max)), new_model_id, loop_id,
    )
    logger.info(
        "Loop #%d cost recalculated: model=%s → $%.2f", loop_id, new_model_id, new_max
    )
    return new_max


def _json_or_null(data: dict | list | None) -> str | None:
    if data is None:
        return None
    import json
    return json.dumps(data, ensure_ascii=False)
