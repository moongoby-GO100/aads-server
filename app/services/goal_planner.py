"""GoalPlanner — CEO 자연어 목표를 마일스톤으로 분해하고 러너와 연결.

목표 원장(goals) → 마일스톤(milestones) → 작업 연결(goal_task_links) → 완료 판정 → 자동 전진.
"""
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MilestoneSpec:
    title: str
    description: str = ""
    sequence: int = 0


class GoalPlanner:
    def __init__(self):
        self._pool = None

    async def _get_pool(self):
        if not self._pool:
            from app.core.db_pool import get_pool
            self._pool = get_pool()
        return self._pool

    async def create_goal(
        self,
        title: str,
        description: str = "",
        project: str = "AADS",
        priority: str = "P2",
        parent_goal_id: Optional[str] = None,
        deadline: Optional[str] = None,
    ) -> str:
        pool = await self._get_pool()
        goal_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO goals (id, title, description, project, priority, parent_goal_id, deadline)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::timestamptz)""",
                goal_id, title, description, project, priority, parent_goal_id, deadline,
            )
        logger.info(f"goal_created: {goal_id} title={title} project={project}")
        return goal_id

    async def add_milestones(self, goal_id: str, milestones: List[MilestoneSpec]) -> List[str]:
        pool = await self._get_pool()
        ids = []
        async with pool.acquire() as conn:
            for ms in milestones:
                ms_id = str(uuid.uuid4())
                await conn.execute(
                    """INSERT INTO milestones (id, goal_id, title, description, sequence_order)
                       VALUES ($1, $2, $3, $4, $5)""",
                    ms_id, goal_id, ms.title, ms.description, ms.sequence,
                )
                ids.append(ms_id)
        return ids

    async def link_task(self, milestone_id: str, task_type: str, task_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO goal_task_links (milestone_id, task_type, task_id)
                   VALUES ($1, $2, $3)""",
                milestone_id, task_type, task_id,
            )

    async def check_milestone_completion(self, milestone_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM goal_task_links WHERE milestone_id = $1", milestone_id
            )
            done = await conn.fetchval(
                "SELECT count(*) FROM goal_task_links WHERE milestone_id = $1 AND status = 'completed'",
                milestone_id,
            )
            return total > 0 and total == done

    async def advance_goal(self, goal_id: str) -> Optional[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            current = await conn.fetchrow(
                """SELECT id, sequence_order FROM milestones
                   WHERE goal_id = $1 AND status = 'in_progress'
                   ORDER BY sequence_order LIMIT 1""",
                goal_id,
            )
            if not current:
                first = await conn.fetchrow(
                    """SELECT id FROM milestones
                       WHERE goal_id = $1 AND status = 'pending'
                       ORDER BY sequence_order LIMIT 1""",
                    goal_id,
                )
                if first:
                    await conn.execute(
                        "UPDATE milestones SET status = 'in_progress', updated_at = NOW() WHERE id = $1",
                        first["id"],
                    )
                    return first["id"]
                return None

            if await self.check_milestone_completion(current["id"]):
                await conn.execute(
                    "UPDATE milestones SET status = 'completed', completed_at = NOW(), updated_at = NOW() WHERE id = $1",
                    current["id"],
                )
                next_ms = await conn.fetchrow(
                    """SELECT id, auto_advance FROM milestones
                       WHERE goal_id = $1 AND sequence_order > $2 AND status = 'pending'
                       ORDER BY sequence_order LIMIT 1""",
                    goal_id, current["sequence_order"],
                )
                if next_ms and next_ms["auto_advance"]:
                    await conn.execute(
                        "UPDATE milestones SET status = 'in_progress', updated_at = NOW() WHERE id = $1",
                        next_ms["id"],
                    )
                    await self._update_goal_progress(goal_id)
                    return next_ms["id"]
                else:
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM milestones WHERE goal_id = $1 AND status != 'completed'",
                        goal_id,
                    )
                    if remaining == 0:
                        await conn.execute(
                            """UPDATE goals SET status = 'completed', progress = 1.0,
                               completed_at = NOW(), updated_at = NOW() WHERE id = $1""",
                            goal_id,
                        )
            return None

    async def _update_goal_progress(self, goal_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM milestones WHERE goal_id = $1", goal_id
            )
            done = await conn.fetchval(
                "SELECT count(*) FROM milestones WHERE goal_id = $1 AND status = 'completed'",
                goal_id,
            )
            if total > 0:
                await conn.execute(
                    "UPDATE goals SET progress = $2, updated_at = NOW() WHERE id = $1",
                    goal_id, round(done / total, 2),
                )

    async def get_goal_status(self, goal_id: str) -> Dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            goal = await conn.fetchrow("SELECT * FROM goals WHERE id = $1", goal_id)
            ms_list = await conn.fetch(
                "SELECT * FROM milestones WHERE goal_id = $1 ORDER BY sequence_order",
                goal_id,
            )
            return {
                "goal": dict(goal) if goal else None,
                "milestones": [dict(m) for m in ms_list],
            }


goal_planner = GoalPlanner()
