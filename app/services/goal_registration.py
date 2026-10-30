"""Atomic chat goal registration with a complete milestone/work hierarchy."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.services import goal_signals
from app.services import goal_work_hierarchy as hierarchy
from app.services.goal_owner_resolver import (
    _SESSION_SQL, goal_owner_binding_policy, resolve_goal_owner,
)


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
    role_key = goal.get("owner_role_key")
    if role_key is not None and (not isinstance(role_key, str) or len(role_key.strip()) > 100):
        errors.append("goal.owner_role_key: 100자 이하 문자열")

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
        role = ms.get("owner_role_key")
        if role is not None and (not isinstance(role, str) or len(role) > 100):
            errors.append(f"{path}.owner_role_key: 100자 이하 문자열")
        if "owner_session_id" in ms:
            try:
                if not isinstance(ms["owner_session_id"], str):
                    raise ValueError
                UUID(ms["owner_session_id"])
            except (ValueError, TypeError):
                errors.append(f"{path}.owner_session_id: UUID 필수")
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


async def register_goal(
    conn: Any,
    inp: dict[str, Any],
    *,
    tenant_id: str,
    session_id: str | None = None,
    actor: hierarchy.ActorScope | None = None,
    activate: bool = True,
    owner_session_id: str | None = None,
    owner_role_key: str | None = None,
    confirm_foreign_owner_session: bool = False,
) -> dict:
    """`session_id` 는 생성 주체(도구 바인딩) 세션이다. 명시 담당은 `owner_session_id` 로만 받는다."""
    errors = validation_errors(inp)
    if errors:
        return {"error": "invalid_goal_hierarchy", "errors": errors}
    goal = inp["goal"]
    project = goal["project"].strip().upper()
    goal_id = str(uuid4())
    counts = {"epic": 0, "story": 0, "task": 0}
    async with conn.transaction():
        # Session identity comes from the executor binding, never tool input claims.
        session_id = session_id or None
        if actor is not None and actor.tenant_id != tenant_id:
            raise HTTPException(403, detail="project_scope_denied")
        if session_id and actor is not None:
            # Validate the requested owner with the authenticated caller's identity.
            # Keep the caller as work-item creator, even for service principals.
            owner = await hierarchy.resolve_actor_scope(
                conn, tenant_id=tenant_id, user_id=actor.user_id,
                actor_session_id=session_id, internal_admin=actor.internal_admin,
            )
            hierarchy.require_project_access(owner, project)
        elif session_id:
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
        elif actor is None or actor.tenant_id != tenant_id:
            raise HTTPException(403, detail="project_scope_denied")
        hierarchy.require_project_access(actor, project)
        owner_session_id = owner_session_id or None
        if owner_session_id and not session_id:
            # REST path: the authenticated user may only name a session they own.
            named = await hierarchy.resolve_actor_scope(
                conn, tenant_id=tenant_id, user_id=actor.user_id,
                actor_session_id=owner_session_id, internal_admin=actor.internal_admin,
            )
            hierarchy.require_project_access(named, project)
        # Fail closed: no goal row (and no milestone/work item) without a confirmed owner.
        owner = await resolve_goal_owner(
            conn,
            tenant_id=tenant_id,
            project=project,
            creator_session_id=session_id,
            owner_role_key=owner_role_key or goal.get("owner_role_key"),
            owner_session_id=owner_session_id,
        )
        if not owner.ok:
            return owner.as_error()
        owner_role, owner_session = owner.owner_role_key, owner.owner_session_id
        warning = None
        if session_id and owner_session != session_id:
            # Chat-tool path only: a goal must show up in the calling chat unless the caller confirms otherwise.
            if not confirm_foreign_owner_session:
                return {
                    "error": "foreign_owner_session_requires_confirmation",
                    "caller_session_id": session_id,
                    "owner_session_id": owner_session,
                    "owner_role_key": owner_role,
                    "reason": "주도 담당 세션이 이 호출 채팅 세션과 다르면 목표가 이 채팅의 목표 띠에 보이지 않는다.",
                    "hint": (
                        "이 채팅이 주도 담당이면 owner_role_key/owner_session_id 를 비우세요. "
                        "실행 담당은 마일스톤 owner_session_id 로 지정하세요. 정말 다른 세션이 "
                        "주도 담당이어야 하면 confirm_foreign_owner_session=true 로 다시 등록하세요."
                    ),
                }
            warning = (
                f"주도 담당(owner_session_id={owner_session})이 호출 세션({session_id})과 달라 "
                "이 채팅의 목표 띠에 표시되지 않을 수 있다."
            )
        # Validate every assignment before any goal/hierarchy writes (fail closed).
        milestone_owners = []
        for i, ms in enumerate(inp["milestones"]):
            role = (ms.get("owner_role_key") or "").strip() or None
            sid = ms.get("owner_session_id")
            if sid:
                sess = await conn.fetchrow(_SESSION_SQL, sid, tenant_id)
                reason = None
                if not sess:
                    reason = "owner_session_not_found"
                elif not goal_owner_binding_policy(
                    project, sess["project_key"], "project_owner", as_lead=True,
                )[0]:
                    reason = "cross_project_owner_denied"
                elif role and role != (sess["role_key"] or ""):
                    reason = "owner_role_mismatch"
                if reason:
                    return {"error": "milestone_owner_out_of_scope",
                            "milestone_index": i, "reason": reason}
                milestone_owners.append((role or sess["role_key"] or None, str(sess["session_id"])))
            elif role:
                milestone_owners.append((role, None))
            else:
                milestone_owners.append((owner_role, owner_session))
        if goal.get("parent_goal_id"):
            parent = await hierarchy.get_goal_scope(
                conn, tenant_id=tenant_id, goal_id=goal["parent_goal_id"]
            )
            if parent["project"] != project:
                raise HTTPException(403, detail="parent_project_mismatch")
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"goal-register:{tenant_id}"
        )
        if activate and goal_signals.is_p0(goal["priority"]):
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
            """INSERT INTO goals (id,tenant_id,project,title,description,success_criteria,priority,parent_goal_id,status,
                   owner_role_key,owner_session_id)
               VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8::uuid,$11,$9,$10::uuid)""",
            goal_id,
            tenant_id,
            project,
            goal["title"],
            goal["description"],
            goal["success_criteria"],
            goal["priority"],
            goal.get("parent_goal_id"),
            owner_role,
            owner_session,
            "active" if activate else "draft",
        )
        milestone_ids = []
        lead_self_assigned_count = 0

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
            inserted_owner_session = await conn.fetchval(
                """INSERT INTO milestones (id,goal_id,tenant_id,project,title,description,completion_criteria,
                   sequence_order,auto_advance,status,owner_role_key,owner_session_id)
                   VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,$9,'pending',$10,$11::uuid) RETURNING owner_session_id::text""",
                ms_id,
                goal_id,
                tenant_id,
                project,
                ms["title"],
                ms["description"],
                ms["completion_criteria"],
                ms["sequence_order"],
                ms["auto_advance"],
                milestone_owners[i][0],
                milestone_owners[i][1],
            )
            # Count successful INSERT assignments, rather than requested milestone ids.
            if inserted_owner_session and inserted_owner_session == str(owner_session):
                lead_self_assigned_count += 1
            for j, epic in enumerate(ms["epics"]):
                await create(epic, "epic", ms_id, f"{i}.{j}")
        if activate:
            first_index = min(
                range(len(inp["milestones"])),
                key=lambda index: inp["milestones"][index]["sequence_order"],
            )
            await conn.execute(
                "UPDATE milestones SET status = 'in_progress', started_at = NOW(), "
                "updated_at = NOW() WHERE id = $1::uuid",
                milestone_ids[first_index],
            )
        links = [(None, "chat_session", owner_session)] + [
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
    # Preserve creation observability now that registration owns all goal writes.
    from app.services.goal_manager import goal_state_machine

    await goal_state_machine._trace(
        "goal_create", project=project, goal_id=goal_id,
        input_summary=f"{project} {goal['priority']} {goal['title']}",
        output_summary="status=active" if activate else "status=draft",
        conn=conn,
    )
    await goal_state_machine._trace_policy(goal_id, project, goal["title"], conn=conn)
    result = {
        "goal_id": goal_id,
        "milestone_count": len(milestone_ids),
        **{f"{k}_count": v for k, v in counts.items()},
        "owner_session_id": owner_session,
        "owner_role_key": owner_role,
    }
    # Warnings use the owners returned by the milestone INSERTs.
    from app.services.milestone_review import _alt_reviewer

    warnings = [
        f"주도가 직접 맡은 마일스톤 {lead_self_assigned_count}개 — "
        "확인자는 같은 프로젝트 다른 담당이 필요"
    ]
    # Newly created goals may not yet have other goal_task_links participants.
    # No alternate is a warning only; registration must remain allowed.
    if not await _alt_reviewer(conn, goal_id, owner_session, owner_role, lead_session=owner_session):
        warnings.append("no_alternate_reviewer")
    result["warnings"] = warnings
    if warning:
        result["warning"] = warning
    return result
