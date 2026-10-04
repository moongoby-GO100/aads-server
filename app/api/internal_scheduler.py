"""MCP 브리지용 예약 작업 내부 엔드포인트.

영속 jobstore(SlotGatedJobStore)는 API 프로세스에만 있다. 세션별 브리지 프로세스는
여기로 위임해야 예약이 apscheduler_jobs 에 남는다. 인증은 require_internal_admin —
브리지는 AADS_MONITOR_KEY(비밀값)로 들어오며 공개 상수 internal-pipeline-call 은 통과하지 못한다.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.ceo_chat_tools_scheduler import (
    list_scheduled_tasks_on,
    schedule_task_on,
    unschedule_task_on,
)
from app.auth import require_internal_admin

router = APIRouter(
    prefix="/internal/scheduler",
    tags=["internal-scheduler"],
    dependencies=[Depends(require_internal_admin)],
)


class ScheduleRequest(BaseModel):
    name: str
    schedule_type: str
    action_type: str
    action_config: Dict[str, Any] = Field(default_factory=dict)
    schedule_config: Optional[Dict[str, Any]] = None
    report_session_id: str = ""
    report_to_session: bool = True
    trigger_session_reaction: bool = True


class UnscheduleRequest(BaseModel):
    name: str


def _api_scheduler(request: Request):
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="scheduler_not_ready")
    return scheduler


@router.post("/schedule")
async def schedule(req: ScheduleRequest, request: Request) -> Dict[str, Any]:
    return await schedule_task_on(
        _api_scheduler(request),
        name=req.name,
        schedule_type=req.schedule_type,
        action_type=req.action_type,
        action_config=req.action_config,
        schedule_config=req.schedule_config,
        report_session_id=req.report_session_id,
        report_to_session=req.report_to_session,
        trigger_session_reaction=req.trigger_session_reaction,
        replace_same_session=True,
    )


@router.post("/unschedule")
async def unschedule(req: UnscheduleRequest, request: Request) -> Dict[str, Any]:
    return await unschedule_task_on(_api_scheduler(request), req.name)


@router.get("/jobs")
async def jobs(request: Request) -> Dict[str, Any]:
    return await list_scheduled_tasks_on(_api_scheduler(request))
