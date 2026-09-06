"""Goal Control Loop — GoalStateMachine.

goals/milestones/goal_task_links 테이블을 조작하여
목표→마일스톤→작업→완료판정→다음단계 자동개시를 구현한다.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


class GoalStateMachine:

    async def _pool(self):
        from app.core.db_pool import get_pool
        return get_pool()

    async def create_goal(
        self,
        project: str,
        title: str,
        priority: str = "P2",
        success_criteria: Optional[str] = None,
        parent_goal_id: Optional[str] = None,
    ) -> dict[str, Any]:
        pool = await self._pool()
        goal_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO goals (id, project, title, priority, description, parent_goal_id, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'draft')
                """,
                uuid.UUID(goal_id), project, title, priority,
                success_criteria, uuid.UUID(parent_goal_id) if parent_goal_id else None,
            )
        return {"goal_id": goal_id, "status": "draft"}

    async def add_milestone(
        self,
        goal_id: str,
        title: str,
        sequence: int,
        completion_criteria: Optional[str] = None,
    ) -> dict[str, Any]:
        pool = await self._pool()
        ms_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO milestones (id, goal_id, title, sequence, description, status)
                VALUES ($1, $2, $3, $4, $5, 'pending')
                """,
                uuid.UUID(ms_id), uuid.UUID(goal_id), title, sequence,
                completion_criteria,
            )
        return {"milestone_id": ms_id, "status": "pending"}

    async def link_task(
        self,
        goal_id: str,
        milestone_id: Optional[str],
        task_type: str,
        task_id: str,
    ) -> dict[str, Any]:
        pool = await self._pool()
        link_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO goal_task_links (id, goal_id, milestone_id, task_type, task_id)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (goal_id, task_type, task_id) DO NOTHING
                """,
                uuid.UUID(link_id), uuid.UUID(goal_id),
                uuid.UUID(milestone_id) if milestone_id else None,
                task_type, task_id,
            )
        return {"link_id": link_id}

    async def check_milestone_completion(self, milestone_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            links = await conn.fetch(
                "SELECT task_type, task_id FROM goal_task_links WHERE milestone_id = $1",
                uuid.UUID(milestone_id),
            )
            if not links:
                return {"milestone_id": milestone_id, "completed": False, "reason": "no_linked_tasks"}

            all_done = True
            for link in links:
                if link["task_type"] == "pipeline_job":
                    row = await conn.fetchrow(
                        "SELECT status FROM pipeline_jobs WHERE job_id = $1",
                        link["task_id"],
                    )
                    if not row or row["status"] != "done":
                        all_done = False
                        break
                elif link["task_type"] == "ohvis_task":
                    row = await conn.fetchrow(
                        "SELECT status FROM ohvis_tasks WHERE id::text = $1",
                        link["task_id"],
                    )
                    if not row or row["status"] != "done":
                        all_done = False
                        break

            if all_done:
                await conn.execute(
                    """
                    UPDATE milestones SET status = 'completed', completed_at = NOW(), updated_at = NOW()
                    WHERE id = $1 AND status != 'completed'
                    """,
                    uuid.UUID(milestone_id),
                )
                ms = await conn.fetchrow(
                    "SELECT goal_id FROM milestones WHERE id = $1",
                    uuid.UUID(milestone_id),
                )
                if ms:
                    await self.check_goal_completion(str(ms["goal_id"]))

            return {"milestone_id": milestone_id, "completed": all_done}

    async def check_goal_completion(self, goal_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            stats = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'completed') AS completed
                FROM milestones WHERE goal_id = $1
                """,
                uuid.UUID(goal_id),
            )
            total = stats["total"] if stats else 0
            completed = stats["completed"] if stats else 0

            if total > 0 and total == completed:
                await conn.execute(
                    """
                    UPDATE goals SET status = 'completed', completed_at = NOW(), updated_at = NOW()
                    WHERE id = $1 AND status != 'completed'
                    """,
                    uuid.UUID(goal_id),
                )
                goal = await conn.fetchrow(
                    "SELECT project, priority FROM goals WHERE id = $1",
                    uuid.UUID(goal_id),
                )
                if goal:
                    next_goal = await conn.fetchrow(
                        """
                        SELECT id FROM goals
                        WHERE project = $1 AND status = 'draft'
                        ORDER BY
                            CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                                          WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 9 END,
                            created_at ASC
                        LIMIT 1
                        """,
                        goal["project"],
                    )
                    if next_goal:
                        await conn.execute(
                            "UPDATE goals SET status = 'active', updated_at = NOW() WHERE id = $1",
                            next_goal["id"],
                        )
                        logger.info("goal_auto_activated: %s", next_goal["id"])

                return {"goal_id": goal_id, "completed": True, "total": total, "completed_count": completed}

            return {"goal_id": goal_id, "completed": False, "total": total, "completed_count": completed}

    async def get_goal_status(self, goal_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            goal = await conn.fetchrow(
                "SELECT id, project, title, priority, status, description, created_at, completed_at FROM goals WHERE id = $1",
                uuid.UUID(goal_id),
            )
            if not goal:
                return {"error": "goal_not_found"}

            milestones = await conn.fetch(
                """
                SELECT id, title, sequence, status, completed_at
                FROM milestones WHERE goal_id = $1 ORDER BY sequence
                """,
                uuid.UUID(goal_id),
            )
            total = len(milestones)
            completed = sum(1 for m in milestones if m["status"] == "completed")

            return {
                "goal_id": str(goal["id"]),
                "project": goal["project"],
                "title": goal["title"],
                "priority": goal["priority"],
                "status": goal["status"],
                "success_criteria": goal["description"],
                "progress": completed / total if total > 0 else 0,
                "milestones_total": total,
                "milestones_completed": completed,
                "milestones": [
                    {
                        "id": str(m["id"]),
                        "title": m["title"],
                        "sequence": m["sequence"],
                        "status": m["status"],
                        "completed_at": m["completed_at"].isoformat() if m["completed_at"] else None,
                    }
                    for m in milestones
                ],
            }


goal_state_machine = GoalStateMachine()
