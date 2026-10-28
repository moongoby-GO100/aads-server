"""goals.progress(0~1 비율)의 단일 계산식.

쓰는 경로가 둘이었고 공식이 달랐다 — goal_workflow_approval.rollup_ancestors 는
완료 work_items / 전체 work_items, goal_manager._update_goal_progress 는 완료 milestones /
전체 milestones. 업무 4건 완료 후 0.09 로 반영된 값을 마일스톤 경로가 0.0 으로 덮었다
(2026-10-28 목표 db7f2a11). 두 경로는 이 함수만 부른다.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MILESTONE_STATS_SQL = """
    SELECT COUNT(*) AS total,
           COUNT(*) FILTER (WHERE status = 'completed') AS completed
    FROM milestones
    WHERE goal_id = $1::uuid
      AND status NOT IN ('cancelled', 'superseded', 'archived')
"""

# 취소된 task 와 acceptance_criteria.optional=true 인 task 는 분자·분모 모두에서 뺀다.
_TASK_STATS_SQL = """
    SELECT count(*) AS counted,
           count(*) FILTER (WHERE status = 'completed') AS finished
    FROM work_items
    WHERE goal_id = $1::uuid AND type = 'task' AND status <> 'cancelled'
      AND ($2::uuid IS NULL OR tenant_id = $2::uuid)
      AND COALESCE((acceptance_criteria->>'optional')::boolean, FALSE) = FALSE
"""


def _ratio(done: int, total: int) -> float:
    return round(min(max(done, 0), total) / total, 2) if total > 0 else 0.0


async def _task_counts(conn: Any, goal_id: str, tenant_id: Optional[str]) -> Optional[tuple[int, int]]:
    """(완료, 집계 대상) task 수. work_items 가 없거나 조회가 실패하면 None — 마일스톤 비율로 물러난다."""
    try:
        row = await conn.fetchrow(_TASK_STATS_SQL, goal_id, tenant_id)
        return int(row["finished"] or 0), int(row["counted"] or 0)
    except Exception as exc:  # noqa: BLE001 — 구 스키마/테스트 더블 호환
        logger.debug("goal_progress_task_stats_unavailable goal=%s: %s", goal_id, str(exc)[:160])
        return None


async def goal_progress_ratio(
    conn: Any, goal_id: str, *, tenant_id: Optional[str] = None,
    milestones: Optional[tuple[int, int]] = None,
) -> float:
    """목표 진행률 0~1.

    work_items(type='task')가 집계 대상으로 있으면 완료 task / (전체 task − cancelled − optional).
    없으면 완료 milestone / 전체 milestone. ``milestones`` 는 (전체, 완료) — 호출부가 이미
    계산했다면 다시 묻지 않는다.
    """
    goal_id = str(goal_id)
    tasks = await _task_counts(conn, goal_id, tenant_id)
    if tasks and tasks[1] > 0:
        return _ratio(tasks[0], tasks[1])
    if milestones is None:
        stats = await conn.fetchrow(_MILESTONE_STATS_SQL, goal_id)
        milestones = (int(stats["total"] or 0), int(stats["completed"] or 0)) if stats else (0, 0)
    total, completed = milestones
    return _ratio(completed, total)
