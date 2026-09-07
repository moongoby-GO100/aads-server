"""Goal Control Loop — GoalStateMachine.

goals/milestones/goal_task_links 테이블을 조작하여
목표→마일스톤→작업→완료판정→다음단계 자동개시를 구현한다.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


_DONE_TASK_STATUSES = {"completed", "done", "approved", "deployed"}
_FAILED_TASK_STATUSES = {"failed", "error", "cancelled", "rejected_done"}


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
                INSERT INTO goals (
                    id, project, title, priority, description, success_criteria,
                    parent_goal_id, status
                )
                VALUES ($1::uuid, $2, $3, $4, $5, $5, $6::uuid, 'draft')
                """,
                goal_id, project, title, priority,
                success_criteria, parent_goal_id,
            )
        return {"goal_id": goal_id, "status": "draft"}

    async def activate_goal(self, goal_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM goals WHERE id = $1::uuid", goal_id,
            )
            if not row:
                return {"error": "goal_not_found"}
            if row["status"] not in ("draft", "paused"):
                return {"error": f"cannot_activate_from_{row['status']}"}
            await conn.execute(
                "UPDATE goals SET status = 'active', updated_at = NOW() WHERE id = $1::uuid",
                goal_id,
            )
            first_ms = await conn.fetchrow(
                """SELECT id FROM milestones
                   WHERE goal_id = $1::uuid AND status = 'pending'
                   ORDER BY sequence_order LIMIT 1""",
                goal_id,
            )
            if first_ms:
                await conn.execute(
                    "UPDATE milestones SET status = 'in_progress', started_at = NOW(), updated_at = NOW() WHERE id = $1",
                    first_ms["id"],
                )
        logger.info("goal_activated: %s", goal_id)
        return {"goal_id": goal_id, "status": "active"}

    async def add_milestone(
        self,
        goal_id: str,
        title: str,
        sequence: int,
        completion_criteria: Optional[str] = None,
        auto_advance: bool = True,
    ) -> dict[str, Any]:
        pool = await self._pool()
        ms_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO milestones (id, goal_id, title, sequence_order, completion_criteria, auto_advance, status)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, 'pending')
                """,
                ms_id, goal_id, title, sequence,
                completion_criteria, auto_advance,
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
            if milestone_id is None:
                milestone_row = await conn.fetchrow(
                    """
                    SELECT id FROM milestones
                    WHERE goal_id = $1::uuid AND status IN ('in_progress', 'pending')
                    ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END,
                             sequence_order
                    LIMIT 1
                    """,
                    goal_id,
                )
                milestone_id = str(milestone_row["id"]) if milestone_row else None

            current_status = "pending"
            if task_type == "pipeline_job":
                job = await conn.fetchrow(
                    "SELECT status FROM pipeline_jobs WHERE job_id = $1",
                    task_id,
                )
                if job:
                    current_status = self._normalize_task_status(job["status"])

            await conn.execute(
                """
                INSERT INTO goal_task_links (id, goal_id, milestone_id, task_type, task_id)
                VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5)
                ON CONFLICT (goal_id, task_type, task_id) WHERE (goal_id IS NOT NULL)
                DO UPDATE SET milestone_id = COALESCE(EXCLUDED.milestone_id, goal_task_links.milestone_id)
                """,
                link_id, goal_id,
                milestone_id,
                task_type, task_id,
            )
            await conn.execute(
                """
                UPDATE goal_task_links
                SET status = $4
                WHERE goal_id = $1::uuid AND task_type = $2 AND task_id = $3
                """,
                goal_id, task_type, task_id, current_status,
            )
        if milestone_id:
            await self.check_milestone_completion(milestone_id)
        return {"link_id": link_id, "milestone_id": milestone_id, "status": current_status}

    async def update_task_status(self, task_type: str, task_id: str, status: str) -> dict[str, Any]:
        pool = await self._pool()
        normalized = self._normalize_task_status(status)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE goal_task_links SET status = $3 WHERE task_type = $1 AND task_id = $2",
                task_type, task_id, normalized,
            )
            links = await conn.fetch(
                """
                SELECT DISTINCT milestone_id, goal_id
                FROM goal_task_links
                WHERE task_type = $1 AND task_id = $2
                """,
                task_type, task_id,
            )
            results = []
            for link in links:
                if normalized == "failed" and link["milestone_id"]:
                    await conn.execute(
                        """
                        UPDATE milestones
                        SET status = 'blocked', updated_at = NOW()
                        WHERE id = $1::uuid AND status IN ('pending', 'in_progress')
                        """,
                        str(link["milestone_id"]),
                    )
                    if link["goal_id"]:
                        await conn.execute(
                            """
                            UPDATE goals
                            SET status = 'blocked', updated_at = NOW()
                            WHERE id = $1::uuid AND status IN ('draft', 'active')
                            """,
                            str(link["goal_id"]),
                        )
                    results.append({
                        "milestone_id": str(link["milestone_id"]),
                        "completed": False,
                        "status": "blocked",
                    })
                elif link["milestone_id"]:
                    r = await self.check_milestone_completion(str(link["milestone_id"]))
                    results.append(r)
                elif link["goal_id"]:
                    r = await self.advance_goal(str(link["goal_id"]))
                    results.append(r)
        return {"updated": len(results), "milestones_checked": results}

    async def check_milestone_completion(self, milestone_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            links = await conn.fetch(
                "SELECT task_type, task_id, status FROM goal_task_links WHERE milestone_id = $1::uuid",
                milestone_id,
            )
            if not links:
                return {"milestone_id": milestone_id, "completed": False, "reason": "no_linked_tasks"}

            all_done = all(
                link["status"] in _DONE_TASK_STATUSES for link in links
            )

            if not all_done:
                for link in links:
                    if link["status"] in _DONE_TASK_STATUSES:
                        continue
                    if link["status"] in _FAILED_TASK_STATUSES:
                        await conn.execute(
                            """
                            UPDATE milestones
                            SET status = 'blocked', updated_at = NOW()
                            WHERE id = $1::uuid AND status IN ('pending', 'in_progress')
                            """,
                            milestone_id,
                        )
                        return {
                            "milestone_id": milestone_id,
                            "completed": False,
                            "status": "blocked",
                            "reason": "linked_task_failed",
                        }
                    if link["task_type"] == "pipeline_job":
                        row = await conn.fetchrow(
                            "SELECT status FROM pipeline_jobs WHERE job_id = $1",
                            link["task_id"],
                        )
                        normalized = self._normalize_task_status(row["status"]) if row else "pending"
                        if normalized == "completed":
                            await conn.execute(
                                "UPDATE goal_task_links SET status = 'completed' WHERE milestone_id = $1::uuid AND task_id = $2",
                                milestone_id, link["task_id"],
                            )
                        elif normalized == "failed":
                            await conn.execute(
                                "UPDATE goal_task_links SET status = 'failed' WHERE milestone_id = $1::uuid AND task_id = $2",
                                milestone_id, link["task_id"],
                            )
                            await conn.execute(
                                """
                                UPDATE milestones
                                SET status = 'blocked', updated_at = NOW()
                                WHERE id = $1::uuid AND status IN ('pending', 'in_progress')
                                """,
                                milestone_id,
                            )
                            return {
                                "milestone_id": milestone_id,
                                "completed": False,
                                "status": "blocked",
                                "reason": "pipeline_job_failed",
                            }
                        else:
                            all_done = False
                            break
                    else:
                        all_done = False
                        break
                else:
                    all_done = True

            if all_done:
                await conn.execute(
                    """
                    UPDATE milestones SET status = 'completed', completed_at = NOW(), updated_at = NOW()
                    WHERE id = $1::uuid AND status != 'completed'
                    """,
                    milestone_id,
                )
                ms = await conn.fetchrow(
                    "SELECT goal_id FROM milestones WHERE id = $1::uuid",
                    milestone_id,
                )
                if ms:
                    await self._advance_after_milestone(str(ms["goal_id"]), milestone_id)

            return {"milestone_id": milestone_id, "completed": all_done}

    async def advance_goal(self, goal_id: str) -> dict[str, Any]:
        """Advance one goal along the goal -> milestone -> task timeline."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            goal = await conn.fetchrow(
                "SELECT id, status FROM goals WHERE id = $1::uuid",
                goal_id,
            )
            if not goal:
                return {"error": "goal_not_found"}
            if goal["status"] in ("completed", "blocked", "cancelled"):
                return {"goal_id": goal_id, "status": goal["status"], "advanced": False}

            current = await conn.fetchrow(
                """
                SELECT id FROM milestones
                WHERE goal_id = $1::uuid AND status = 'in_progress'
                ORDER BY sequence_order
                LIMIT 1
                """,
                goal_id,
            )
            if current:
                checked = await self.check_milestone_completion(str(current["id"]))
                await self._update_goal_progress(goal_id)
                return {"goal_id": goal_id, "advanced": checked.get("completed", False), "current": checked}

            pending = await conn.fetchrow(
                """
                SELECT id FROM milestones
                WHERE goal_id = $1::uuid AND status = 'pending'
                ORDER BY sequence_order
                LIMIT 1
                """,
                goal_id,
            )
            if pending:
                if goal["status"] == "draft":
                    await conn.execute(
                        "UPDATE goals SET status = 'active', updated_at = NOW() WHERE id = $1::uuid",
                        goal_id,
                    )
                await conn.execute(
                    """
                    UPDATE milestones
                    SET status = 'in_progress', started_at = COALESCE(started_at, NOW()), updated_at = NOW()
                    WHERE id = $1::uuid
                    """,
                    str(pending["id"]),
                )
                await self._update_goal_progress(goal_id)
                return {
                    "goal_id": goal_id,
                    "advanced": True,
                    "started_milestone_id": str(pending["id"]),
                }

            await self._update_goal_progress(goal_id)
            status = await conn.fetchval("SELECT status FROM goals WHERE id = $1::uuid", goal_id)
            return {"goal_id": goal_id, "status": status, "advanced": status == "completed"}

    async def advance_active_goals(self, project: Optional[str] = None) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            if project:
                rows = await conn.fetch(
                    "SELECT id FROM goals WHERE project = $1 AND status IN ('draft', 'active') ORDER BY created_at",
                    project,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id FROM goals WHERE status IN ('draft', 'active') ORDER BY project, created_at"
                )
        results = [await self.advance_goal(str(row["id"])) for row in rows]
        return {"checked": len(results), "results": results}

    async def _advance_after_milestone(self, goal_id: str, completed_milestone_id: str) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            completed_ms = await conn.fetchrow(
                "SELECT sequence_order FROM milestones WHERE id = $1::uuid",
                completed_milestone_id,
            )
            seq = completed_ms["sequence_order"] if completed_ms else -1

            next_ms = await conn.fetchrow(
                """SELECT id, auto_advance FROM milestones
                   WHERE goal_id = $1::uuid AND sequence_order > $2 AND status = 'pending'
                   ORDER BY sequence_order LIMIT 1""",
                goal_id, seq,
            )
            if next_ms and next_ms["auto_advance"]:
                await conn.execute(
                    "UPDATE milestones SET status = 'in_progress', started_at = NOW(), updated_at = NOW() WHERE id = $1",
                    next_ms["id"],
                )
                logger.info("milestone_auto_advanced: %s → %s", completed_milestone_id, next_ms["id"])

            await self._update_goal_progress(goal_id)

    async def _update_goal_progress(self, goal_id: str) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            stats = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'completed') AS completed
                FROM milestones WHERE goal_id = $1::uuid
                """,
                goal_id,
            )
            total = stats["total"] if stats else 0
            completed = stats["completed"] if stats else 0
            progress = round(completed / total, 2) if total > 0 else 0.0

            if total > 0 and total == completed:
                await conn.execute(
                    """UPDATE goals SET status = 'completed', progress = 1.0,
                       completed_at = NOW(), updated_at = NOW() WHERE id = $1::uuid AND status != 'completed'""",
                    goal_id,
                )
                goal = await conn.fetchrow(
                    "SELECT project FROM goals WHERE id = $1::uuid", goal_id,
                )
                if goal:
                    next_goal = await conn.fetchrow(
                        """SELECT id FROM goals
                           WHERE project = $1 AND status = 'draft'
                           ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                                                   WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 9 END,
                                    created_at ASC
                           LIMIT 1""",
                        goal["project"],
                    )
                    if next_goal:
                        await conn.execute(
                        "UPDATE goals SET status = 'active', updated_at = NOW() WHERE id = $1",
                            next_goal["id"],
                        )
                        logger.info("goal_auto_activated: %s (after %s completed)", next_goal["id"], goal_id)
            else:
                await conn.execute(
                    "UPDATE goals SET progress = $2, updated_at = NOW() WHERE id = $1::uuid",
                    goal_id, progress,
                )

    async def check_goal_completion(self, goal_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            stats = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'completed') AS completed
                FROM milestones WHERE goal_id = $1::uuid
                """,
                goal_id,
            )
            total = stats["total"] if stats else 0
            completed = stats["completed"] if stats else 0
            return {"goal_id": goal_id, "completed": total > 0 and total == completed, "total": total, "completed_count": completed}

    async def get_goal_status(self, goal_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            goal = await conn.fetchrow(
                """
                SELECT id, project, title, priority, status, description,
                       success_criteria, progress, created_at, completed_at
                FROM goals WHERE id = $1::uuid
                """,
                goal_id,
            )
            if not goal:
                return {"error": "goal_not_found"}

            milestones = await conn.fetch(
                """
                SELECT id, title, sequence_order, status, auto_advance, completion_criteria, started_at, completed_at
                FROM milestones WHERE goal_id = $1::uuid ORDER BY sequence_order
                """,
                goal_id,
            )
            tasks = await conn.fetch(
                "SELECT id, milestone_id, task_type, task_id, status FROM goal_task_links WHERE goal_id = $1::uuid",
                goal_id,
            )
            task_map: dict[str, list] = {}
            for t in tasks:
                ms_key = str(t["milestone_id"]) if t["milestone_id"] else "unlinked"
                task_map.setdefault(ms_key, []).append({
                    "task_type": t["task_type"],
                    "task_id": t["task_id"],
                    "status": t["status"],
                })

            total = len(milestones)
            completed = sum(1 for m in milestones if m["status"] == "completed")

            return {
                "goal_id": str(goal["id"]),
                "project": goal["project"],
                "title": goal["title"],
                "priority": goal["priority"],
                "status": goal["status"],
                "description": goal["description"],
                "success_criteria": goal["success_criteria"] or goal["description"],
                "progress": float(goal["progress"]) if goal["progress"] else (completed / total if total > 0 else 0),
                "milestones_total": total,
                "milestones_completed": completed,
                "milestones": [
                    {
                        "id": str(m["id"]),
                        "title": m["title"],
                        "sequence": m["sequence_order"],
                        "status": m["status"],
                        "auto_advance": m["auto_advance"],
                        "completion_criteria": m["completion_criteria"],
                        "started_at": m["started_at"].isoformat() if m["started_at"] else None,
                        "completed_at": m["completed_at"].isoformat() if m["completed_at"] else None,
                        "tasks": task_map.get(str(m["id"]), []),
                    }
                    for m in milestones
                ],
            }

    async def list_goals(self, project: Optional[str] = None) -> list[dict[str, Any]]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            if project:
                rows = await conn.fetch(
                    """SELECT id, project, title, priority, status, progress, created_at, completed_at
                       FROM goals WHERE project = $1 ORDER BY
                       CASE status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 WHEN 'paused' THEN 2 WHEN 'blocked' THEN 3 WHEN 'completed' THEN 4 ELSE 9 END,
                       created_at DESC""",
                    project,
                )
            else:
                rows = await conn.fetch(
                    """SELECT id, project, title, priority, status, progress, created_at, completed_at
                       FROM goals ORDER BY
                       CASE status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 WHEN 'paused' THEN 2 WHEN 'blocked' THEN 3 WHEN 'completed' THEN 4 ELSE 9 END,
                       created_at DESC"""
                )
        return [
            {
                "goal_id": str(r["id"]),
                "project": r["project"],
                "title": r["title"],
                "priority": r["priority"],
                "status": r["status"],
                "progress": float(r["progress"]) if r["progress"] else 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ]

    async def update_goal(self, goal_id: str, **kwargs) -> dict[str, Any]:
        pool = await self._pool()
        allowed = {"title", "priority", "description", "success_criteria", "status", "deadline"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return {"error": "no_valid_fields"}
        set_parts = [f"{k} = ${i+2}" for i, k in enumerate(updates)]
        set_parts.append("updated_at = NOW()")
        sql = f"UPDATE goals SET {', '.join(set_parts)} WHERE id = $1::uuid"
        async with pool.acquire() as conn:
            await conn.execute(sql, goal_id, *updates.values())
        return {"goal_id": goal_id, "updated": list(updates.keys())}

    def _normalize_task_status(self, status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized in _DONE_TASK_STATUSES:
            return "completed"
        if normalized in _FAILED_TASK_STATUSES:
            return "failed"
        return normalized or "pending"


goal_state_machine = GoalStateMachine()
