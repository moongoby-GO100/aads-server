"""Goals API — 목표 Control Loop 엔드포인트."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()


class MilestoneCreateRequest(BaseModel):
    title: str
    sequence: int
    completion_criteria: Optional[str] = None
    auto_advance: bool = True


class GoalCreateRequest(BaseModel):
    project: str
    title: str
    priority: str = "P2"
    success_criteria: Optional[str] = None
    parent_goal_id: Optional[str] = None
    milestones: Optional[list[MilestoneCreateRequest]] = None
    activate: bool = False


class LinkTaskRequest(BaseModel):
    milestone_id: Optional[str] = None
    task_type: str
    task_id: str


class GoalUpdateRequest(BaseModel):
    title: Optional[str] = None
    priority: Optional[str] = None
    description: Optional[str] = None
    success_criteria: Optional[str] = None
    status: Optional[str] = None
    deadline: Optional[str] = None


class TaskStatusRequest(BaseModel):
    task_type: str
    task_id: str
    status: str
    # phase 는 선택 — terminated/review_failed/blocked_dependency 같은 별칭을
    # status 와 함께 넘기면 정규화가 실패 상태를 놓치지 않는다 (하위호환).
    phase: Optional[str] = None


@router.get("/goals")
async def list_goals(project: Optional[str] = Query(None)):
    from app.services.goal_manager import goal_state_machine
    return await goal_state_machine.list_goals(project)


@router.post("/goals")
async def create_goal(req: GoalCreateRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.create_goal(
        project=req.project,
        title=req.title,
        priority=req.priority,
        success_criteria=req.success_criteria,
        parent_goal_id=req.parent_goal_id,
    )
    if req.milestones:
        for ms in req.milestones:
            await goal_state_machine.add_milestone(
                goal_id=result["goal_id"],
                title=ms.title,
                sequence=ms.sequence,
                completion_criteria=ms.completion_criteria,
                auto_advance=ms.auto_advance,
            )
    if req.activate:
        result = await goal_state_machine.activate_goal(result["goal_id"])
    return result


@router.get("/goals/{goal_id}/status")
async def goal_status(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.get_goal_status(goal_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/goals/{goal_id}/activate")
async def activate_goal(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.activate_goal(goal_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/goals/{goal_id}/milestones")
async def add_milestone(goal_id: str, req: MilestoneCreateRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.add_milestone(
        goal_id=goal_id,
        title=req.title,
        sequence=req.sequence,
        completion_criteria=req.completion_criteria,
        auto_advance=req.auto_advance,
    )
    return result


@router.post("/goals/{goal_id}/link-task")
async def link_task(goal_id: str, req: LinkTaskRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.link_task(
        goal_id=goal_id,
        milestone_id=req.milestone_id,
        task_type=req.task_type,
        task_id=req.task_id,
    )
    return result


@router.post("/goals/{goal_id}/check-completion")
async def check_completion(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.check_goal_completion(goal_id)
    return result


@router.post("/goals/{goal_id}/advance")
async def advance_goal(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.advance_goal(goal_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/goals/advance")
async def advance_active_goals(project: Optional[str] = Query(None)):
    from app.services.goal_manager import goal_state_machine
    return await goal_state_machine.advance_active_goals(project)


@router.post("/goals/task-status")
async def update_task_status(req: TaskStatusRequest):
    from app.services.goal_manager import goal_state_machine
    return await goal_state_machine.update_task_status_with_phase(
        task_type=req.task_type,
        task_id=req.task_id,
        status=req.status,
        phase=req.phase,
    )


@router.post("/goals/reconcile")
async def reconcile_goal_links(
    project: Optional[str] = Query(None, description="프로젝트 코드 (미지정 시 전체)"),
    dry_run: bool = Query(True, description="true(기본)면 계획만 반환하고 DB 를 쓰지 않는다"),
    limit: int = Query(200, ge=1, le=2000, description="한 번에 수정할 최대 링크 수"),
    detach_legacy: bool = Query(
        False,
        description="프로젝트만 보고 붙은 레거시 링크까지 회수 — 운영자가 명시할 때만",
    ),
):
    """goal_task_links 를 pipeline_jobs 기준으로 재조정한다.

    stale / misbound / orphan / legacy_unverified 건수를 보고하고,
    dry_run=false 일 때만 limit 범위 안에서 복구한다. 행을 삭제하지 않으며,
    같은 입력으로 다시 돌리면 repaired=0 이다(멱등).
    """
    from app.services.goal_link_reconciler import reconcile

    return await reconcile(
        project,
        dry_run=dry_run,
        limit=limit,
        detach_legacy=detach_legacy,
    )


@router.put("/goals/{goal_id}")
async def update_goal(goal_id: str, req: GoalUpdateRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.update_goal(
        goal_id, **req.model_dump(exclude_none=True),
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
