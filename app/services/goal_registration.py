"""Atomic chat goal registration with a complete milestone/work hierarchy."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.services import goal_signals
from app.services import goal_work_hierarchy as hierarchy


def validation_errors(inp: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def title(node: dict, path: str) -> None:
        if not text(node.get("title")) or not re.search("[가-힣ㄱ-ㅎㅏ-ㅣ]", node["title"]):
            errors.append(f"{path}.title: 한글 포함 필수")

    def children(node: dict, key: str, path: str, minimum: int = 1) -> list:
        value = node.get(key)
        if not isinstance(value, list):
            errors.append(f"{path}.{key}: {minimum}개 이상 필수")
            return []
        if len(value) < minimum:
            errors.append(f"{path}.{key}: {minimum}개 이상 필수")
        return value

    goal = inp.get("goal")
    if not isinstance(goal, dict):
        goal = {}
    title(goal, "goal")
    for key in ("project", "description", "success_criteria", "priority"):
        if not text(goal.get(key)):
            errors.append(f"goal.{key}: 필수")
    if goal.get("priority") not in ("P0", "P0-CRITICAL", "P1", "P2", "P3"):
        errors.append("goal.priority: 지원하지 않는 우선순위")
    if goal.get("parent_goal_id"):
        try:
            UUID(str(goal["parent_goal_id"]))
        except (ValueError, TypeError):
            errors.append("goal.parent_goal_id: UUID 필수")

    def work(node: Any, path: str, child_key: str | None) -> None:
        if not isinstance(node, dict):
            errors.append(f"{path}: 객체 필수")
            return
        title(node, path)
        criteria = node.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria or not all(text(c) for c in criteria):
            errors.append(f"{path}.acceptance_criteria: 비어 있지 않은 기준 1개 이상 필수")
        if child_key:
            for i, child in enumerate(children(node, child_key, path)):
                work(child, f"{path}.{child_key}[{i}]", "tasks" if child_key == "stories" else None)

    milestones = children(inp, "milestones", "input", 3)
    for i, ms in enumerate(milestones):
        path = f"milestones[{i}]"
        if not isinstance(ms, dict):
            errors.append(f"{path}: 객체 필수")
            continue
        title(ms, path)
        for key in ("description", "completion_criteria"):
            if not text(ms.get(key)):
                errors.append(f"{path}.{key}: 필수")
        if type(ms.get("sequence_order")) is not int:
            errors.append(f"{path}.sequence_order: 정수 필수")
        if type(ms.get("auto_advance")) is not bool:
            errors.append(f"{path}.auto_advance: boolean 필수")
        for j, epic in enumerate(children(ms, "epics", path)):
            work(epic, f"{path}.epics[{j}]", "stories")
    jobs = inp.get("linked_jobs", [])
    if not isinstance(jobs, list):
        errors.append("linked_jobs: 배열 필수")
    else:
        for i, job in enumerate(jobs):
            if (
                not isinstance(job, dict)
                or type(job.get("milestone_index")) is not int
                or not 0 <= job["milestone_index"] < len(milestones)
                or not text(job.get("job_id"))
            ):
                errors.append(f"linked_jobs[{i}]: 유효한 milestone_index(0부터)와 job_id 필수")
    return errors


async def register_goal(conn: Any, inp: dict[str, Any], *, tenant_id: str, session_id: str) -> dict:
    errors = validation_errors(inp)
    if errors:
        return {"error": "invalid_goal_hierarchy", "errors": errors}
    goal = inp["goal"]
    project = goal["project"].strip().upper()
    goal_id = str(uuid4())
    counts = {"epic": 0, "story": 0, "task": 0}
    async with conn.transaction():
        # Session identity comes from the executor binding, never tool input claims.
        session = await conn.fetchrow(
            "SELECT user_id FROM chat_sessions WHERE id=$1::uuid AND tenant_id=$2::uuid",
            session_id,
            tenant_id,
        )
        if not session:
            raise HTTPException(403, detail="project_scope_denied")
        actor = await hierarchy.resolve_actor_scope(
            conn,
            tenant_id=tenant_id,
            user_id=str(session["user_id"] or ""),
            actor_session_id=session_id,
        )
        hierarchy.require_project_access(actor, project)
        if goal.get("parent_goal_id"):
            parent = await hierarchy.get_goal_scope(
                conn, tenant_id=tenant_id, goal_id=goal["parent_goal_id"]
            )
            if parent["project"] != project:
                raise HTTPException(403, detail="parent_project_mismatch")
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"goal-register:{tenant_id}"
        )
        if goal_signals.is_p0(goal["priority"]):
            others = await goal_signals.active_p0_goals(conn, tenant_id)
            if goal_signals.p0_cap_violation(
                current_priority=None,
                current_status=None,
                new_priority=goal["priority"],
                new_status="active",
                others=others,
            ):
                return {
                    "error": "p0_cap_exceeded",
                    "cap": goal_signals.P0_CAP,
                    "current_p0": others,
                }
        for job in inp.get("linked_jobs", []):
            if not await conn.fetchval(
                "SELECT 1 FROM pipeline_jobs WHERE job_id=$1 AND tenant_id=$2::uuid AND project=$3",
                job["job_id"],
                tenant_id,
                project,
            ):
                raise HTTPException(422, detail="linked_job_not_found_in_project")
        await conn.execute(
            """INSERT INTO goals (id,tenant_id,project,title,description,success_criteria,priority,parent_goal_id,status)
               VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8::uuid,'active')""",
            goal_id,
            tenant_id,
            project,
            goal["title"],
            goal["description"],
            goal["success_criteria"],
            goal["priority"],
            goal.get("parent_goal_id"),
        )
        milestone_ids = []

        async def create(
            node: dict, kind: str, ms_id: str, path: str, parent_id: str | None = None
        ) -> None:
            item, _ = await hierarchy.create_work_item(
                conn,
                tenant_id=tenant_id,
                actor=actor,
                goal_id=goal_id,
                payload={
                    "type": kind,
                    "milestone_id": ms_id,
                    "parent_id": parent_id,
                    "title": node["title"],
                    "acceptance_criteria": node["acceptance_criteria"],
                    "idempotency_key": f"goal-register:{goal_id}:{path}",
                },
            )
            counts[kind] += 1
            child_key, child_type = {
                "epic": ("stories", "story"),
                "story": ("tasks", "task"),
                "task": (None, None),
            }[kind]
            for i, child in enumerate(node.get(child_key, []) if child_key else []):
                await create(child, child_type, ms_id, f"{path}.{i}", str(item["id"]))

        for i, ms in enumerate(inp["milestones"]):
            ms_id = str(uuid4())
            milestone_ids.append(ms_id)
            await conn.execute(
                """INSERT INTO milestones (id,goal_id,tenant_id,project,title,description,completion_criteria,
                   sequence_order,auto_advance,status,owner_role_key)
                   VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,$9,'pending',$10)""",
                ms_id,
                goal_id,
                tenant_id,
                project,
                ms["title"],
                ms["description"],
                ms["completion_criteria"],
                ms["sequence_order"],
                ms["auto_advance"],
                goal.get("owner_role_key"),
            )
            for j, epic in enumerate(ms["epics"]):
                await create(epic, "epic", ms_id, f"{i}.{j}")
        links = [(None, "chat_session", session_id)] + [
            (milestone_ids[j["milestone_index"]], "pipeline_job", j["job_id"])
            for j in inp.get("linked_jobs", [])
        ]
        for ms_id, kind, task_id in links:
            await conn.execute(
                """INSERT INTO goal_task_links (goal_id,milestone_id,tenant_id,task_type,task_id,status)
                   VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,'active')
                   ON CONFLICT (goal_id,task_type,task_id) WHERE goal_id IS NOT NULL DO NOTHING""",
                goal_id,
                ms_id,
                tenant_id,
                kind,
                task_id,
            )
    return {
        "goal_id": goal_id,
        "milestone_count": len(milestone_ids),
        **{f"{k}_count": v for k, v in counts.items()},
    }
