"""프로젝트 생성 + 상태 조회."""
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

router = APIRouter()


class CreateProjectRequest(BaseModel):
    description: str


class CreateProjectResponse(BaseModel):
    project_id: str
    status: str
    checkpoint_stage: str
    interrupt_payload: dict | None = None


@router.post("/projects", response_model=CreateProjectResponse)
async def create_project(req: CreateProjectRequest):
    """프로젝트 생성 → PM 노드까지 실행 → interrupt에서 멈춤."""
    from app.main import app_state

    graph = app_state.get("graph")
    if not graph:
        raise HTTPException(503, "Graph not ready")

    project_id = str(uuid.uuid4())[:8]
    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "messages": [HumanMessage(content=req.description)],
        "current_task": None,
        "task_queue": [],
        "next_agent": None,
        "active_agents": [],
        "checkpoint_stage": "requirements",
        "approved_stages": [],
        "revision_count": 0,
        "llm_calls_count": 0,
        "total_cost_usd": 0.0,
        "cost_breakdown": {},
        "generated_files": [],
        "sandbox_results": [],
        "project_id": project_id,
        "created_at": datetime.utcnow().isoformat(),
        "iteration_count": 0,
        "error_log": [],
    }

    # 그래프 실행 → PM의 interrupt()에서 멈춤
    result = await graph.ainvoke(initial_state, config=config)

    # interrupt 정보 추출
    interrupt_payload = None
    if "__interrupt__" in result:
        interrupts = result["__interrupt__"]
        if interrupts:
            interrupt_payload = {
                "value": interrupts[0].value,
                "id": str(interrupts[0].id) if hasattr(interrupts[0], "id") else None,
            }

    return CreateProjectResponse(
        project_id=project_id,
        status="checkpoint_pending",
        checkpoint_stage="requirements",
        interrupt_payload=interrupt_payload,
    )


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    """프로젝트 상태 조회."""
    from app.main import app_state

    graph = app_state.get("graph")
    if not graph:
        raise HTTPException(503, "Graph not ready")

    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    state = await graph.aget_state(config)
    if not state or not state.values:
        raise HTTPException(404, "Project not found")

    return {
        "project_id": project_id,
        "checkpoint_stage": state.values.get("checkpoint_stage"),
        "current_task": state.values.get("current_task"),
        "generated_files": state.values.get("generated_files", []),
        "sandbox_results": state.values.get("sandbox_results", []),
        "llm_calls_count": state.values.get("llm_calls_count", 0),
        "total_cost_usd": state.values.get("total_cost_usd", 0.0),
        "error_log": state.values.get("error_log", []),
    }


@router.get("/projects/{project_id}/costs")
async def get_project_costs(project_id: str):
    """프로젝트 비용 상세 조회 (에이전트별 토큰 비용)."""
    from app.main import app_state
    from app.services.cost_tracker import get_project_costs as _get_costs

    graph = app_state.get("graph")
    if not graph:
        raise HTTPException(503, "Graph not ready")

    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    state = await graph.aget_state(config)
    if not state or not state.values:
        raise HTTPException(404, "Project not found")

    breakdown = state.values.get("cost_breakdown", {})
    result = await _get_costs(project_id, breakdown)
    result["llm_calls_count"] = state.values.get("llm_calls_count", 0)
    return result
