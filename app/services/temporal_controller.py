"""시간축 컨트롤러 — 마일스톤 완료 판정 + 자동 다음 단계 진행."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TemporalController:
    """목표-마일스톤 체인의 시간축 관리 + 자동 진행 엔진."""

    def __init__(self, check_interval: int = 60) -> None:
        self._check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("temporal_controller_started interval=%ds", self._check_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_and_advance()
            except Exception as e:
                logger.warning("temporal_check_error: %s", e)
            await asyncio.sleep(self._check_interval)

    async def _check_and_advance(self) -> None:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            active_goals = await conn.fetch(
                "SELECT id, project FROM goals WHERE status = 'active'"
            )
            for goal in active_goals:
                await self._check_goal(conn, goal["id"], goal["project"])

    async def _check_goal(self, conn, goal_id: str, project: str) -> None:
        milestones = await conn.fetch(
            "SELECT id, title, status, due_date FROM milestones "
            "WHERE goal_id = $1 ORDER BY sequence_order",
            goal_id,
        )
        if not milestones:
            return

        current = None
        next_pending = None
        for m in milestones:
            if m["status"] == "in_progress":
                current = m
            elif m["status"] == "pending" and next_pending is None:
                next_pending = m

        if not current:
            if next_pending:
                await conn.execute(
                    "UPDATE milestones SET status = 'in_progress', started_at = NOW() WHERE id = $1",
                    next_pending["id"],
                )
                logger.info("milestone_auto_started goal=%s milestone=%s", goal_id, next_pending["id"])
            return

        linked_tasks = await conn.fetch(
            "SELECT task_type, task_id FROM goal_task_links WHERE milestone_id = $1",
            current["id"],
        )
        if not linked_tasks:
            return

        all_done = True
        for lt in linked_tasks:
            if lt["task_type"] == "runner":
                row = await conn.fetchrow(
                    "SELECT status FROM ohvis_tasks WHERE job_id = $1",
                    lt["task_id"],
                )
                if not row or row["status"] not in ("done", "deployed"):
                    all_done = False
                    break
            elif lt["task_type"] == "manual":
                row = await conn.fetchrow(
                    "SELECT status FROM goal_task_links WHERE milestone_id = $1 AND task_id = $2",
                    current["id"], lt["task_id"],
                )
                if not row or row.get("status") != "done":
                    all_done = False
                    break

        if all_done:
            await conn.execute(
                "UPDATE milestones SET status = 'completed', completed_at = NOW() WHERE id = $1",
                current["id"],
            )
            logger.info("milestone_completed goal=%s milestone=%s", goal_id, current["id"])

            if next_pending:
                await conn.execute(
                    "UPDATE milestones SET status = 'in_progress', started_at = NOW() WHERE id = $1",
                    next_pending["id"],
                )
                logger.info("milestone_auto_advanced goal=%s next=%s", goal_id, next_pending["id"])

            all_milestones_done = all(
                m["status"] == "completed" for m in milestones if m["id"] != current["id"]
            ) and not next_pending
            if all_milestones_done:
                await conn.execute(
                    "UPDATE goals SET status = 'completed', updated_at = NOW() WHERE id = $1",
                    goal_id,
                )
                logger.info("goal_completed id=%s project=%s", goal_id, project)

    async def check_overdue(self) -> List[Dict[str, Any]]:
        from app.core.db_pool import get_pool
        pool = get_pool()
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.id, m.title, m.due_date, g.project, g.title as goal_title "
                "FROM milestones m JOIN goals g ON m.goal_id = g.id "
                "WHERE m.status = 'in_progress' AND m.due_date IS NOT NULL AND m.due_date < $1",
                now,
            )
            return [dict(r) for r in rows]


temporal_controller = TemporalController()
