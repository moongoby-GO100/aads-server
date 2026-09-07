"""GoalPlanner facade for the Goal Control Loop.

This module keeps the older planner import path while delegating writes and
timeline advancement to GoalStateMachine, the single source of truth.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MilestoneSpec:
    title: str
    description: str = ""
    sequence: int = 0
    completion_criteria: str = ""
    auto_advance: bool = True


class GoalPlanner:
    async def create_goal(
        self,
        title: str,
        description: str = "",
        project: str = "AADS",
        priority: str = "P2",
        parent_goal_id: Optional[str] = None,
        deadline: Optional[str] = None,
    ) -> str:
        from app.services.goal_manager import goal_state_machine

        result = await goal_state_machine.create_goal(
            project=project,
            title=title,
            priority=priority,
            success_criteria=description,
            parent_goal_id=parent_goal_id,
        )
        if deadline:
            await goal_state_machine.update_goal(result["goal_id"], deadline=deadline)
        logger.info("goal_created: %s title=%s project=%s", result["goal_id"], title, project)
        return result["goal_id"]

    async def add_milestones(self, goal_id: str, milestones: list[MilestoneSpec]) -> list[str]:
        from app.services.goal_manager import goal_state_machine

        ids: list[str] = []
        for ms in milestones:
            result = await goal_state_machine.add_milestone(
                goal_id=goal_id,
                title=ms.title,
                sequence=ms.sequence,
                completion_criteria=ms.completion_criteria or ms.description,
                auto_advance=ms.auto_advance,
            )
            ids.append(result["milestone_id"])
        return ids

    async def create_plan(
        self,
        title: str,
        milestones: list[MilestoneSpec],
        description: str = "",
        project: str = "AADS",
        priority: str = "P2",
        activate: bool = False,
    ) -> dict[str, Any]:
        from app.services.goal_manager import goal_state_machine

        goal_id = await self.create_goal(
            title=title,
            description=description,
            project=project,
            priority=priority,
        )
        milestone_ids = await self.add_milestones(goal_id, milestones)
        status: dict[str, Any] | None = None
        if activate:
            status = await goal_state_machine.activate_goal(goal_id)
        return {
            "goal_id": goal_id,
            "milestone_ids": milestone_ids,
            "status": status["status"] if status else "draft",
        }

    async def link_task(
        self,
        milestone_id: str,
        task_type: str,
        task_id: str,
        goal_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from app.services.goal_manager import goal_state_machine

        if goal_id is None:
            goal_id = await self._goal_id_for_milestone(milestone_id)
        if not goal_id:
            return {"error": "milestone_not_found"}
        return await goal_state_machine.link_task(
            goal_id=goal_id,
            milestone_id=milestone_id,
            task_type=task_type,
            task_id=task_id,
        )

    async def check_milestone_completion(self, milestone_id: str) -> bool:
        from app.services.goal_manager import goal_state_machine

        result = await goal_state_machine.check_milestone_completion(milestone_id)
        return bool(result.get("completed"))

    async def advance_goal(self, goal_id: str) -> Optional[str]:
        from app.services.goal_manager import goal_state_machine

        before = await goal_state_machine.get_goal_status(goal_id)
        result = await goal_state_machine.advance_goal(goal_id)
        if "started_milestone_id" in result:
            return str(result["started_milestone_id"])
        after = await goal_state_machine.get_goal_status(goal_id)
        before_current = self._current_milestone_id(before)
        after_current = self._current_milestone_id(after)
        if after_current and after_current != before_current:
            return after_current
        return None

    async def get_goal_status(self, goal_id: str) -> dict[str, Any]:
        from app.services.goal_manager import goal_state_machine

        return await goal_state_machine.get_goal_status(goal_id)

    async def _goal_id_for_milestone(self, milestone_id: str) -> Optional[str]:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            value = await conn.fetchval(
                "SELECT goal_id::text FROM milestones WHERE id = $1::uuid",
                milestone_id,
            )
        return str(value) if value else None

    def _current_milestone_id(self, status: dict[str, Any]) -> Optional[str]:
        for milestone in status.get("milestones", []) if status else []:
            if milestone.get("status") == "in_progress":
                return str(milestone.get("id"))
        return None


goal_planner = GoalPlanner()
