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


class LinkReconcileRequest(BaseModel):
    """goal_task_links 정합화 요청. 기본은 dry-run (쓰기 없음)."""
    project: Optional[str] = None
    dry_run: bool = True
    limit: int = 200
    detach_misbound: bool = False   # 프로젝트 불일치 링크의 마일스톤 분리 (행 보존)
    mark_orphans: bool = False      # pipeline_jobs 행이 없는 링크를 orphaned 로 표시


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
    from app.services.goal_binding import BIND_SOURCE_EXPLICIT
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.link_task(
        goal_id=goal_id,
        milestone_id=req.milestone_id,
        task_type=req.task_type,
        task_id=req.task_id,
        bind_source=BIND_SOURCE_EXPLICIT,
    )
    return result


@router.get("/goals/links/audit")
async def audit_goal_links(project: Optional[str] = Query(None)):
    """goal_task_links ↔ pipeline_jobs 불일치 감사 (읽기 전용)."""
    from app.services import goal_link_reconciler
    return await goal_link_reconciler.scan(project)


@router.post("/goals/links/reconcile")
async def reconcile_goal_links(req: LinkReconcileRequest):
    """정합화 실행. `dry_run=true`(기본)면 계획만 돌려주고 아무것도 쓰지 않는다."""
    from app.services import goal_link_reconciler
    if req.limit < 1 or req.limit > 1000:
        raise HTTPException(status_code=400, detail="limit은 1..1000 범위여야 합니다")
    return await goal_link_reconciler.reconcile(
        req.project,
        dry_run=req.dry_run,
        limit=req.limit,
        detach_misbound=req.detach_misbound,
        mark_orphans=req.mark_orphans,
    )


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
    return await goal_state_machine.update_task_status(
        task_type=req.task_type,
        task_id=req.task_id,
        status=req.status,
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
