"""프로젝트 생성 + 상태 조회."""
import asyncio
import uuid
from datetime import datetime
import os

from fastapi import APIRouter, HTTPException, Header, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

async def _get_healing_engine():
    """ProjectHealingEngine singleton (DB URL이 없으면 None)."""
    from app.main import app_state
    engine = app_state.get("project_healing")
    if engine is not None:
        return engine
    db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
    if not db_url:
        app_state["project_healing"] = None
        return None
    try:
        from app.services.project_healing import ProjectHealingEngine
        engine = ProjectHealingEngine(db_url)
        app_state["project_healing"] = engine
        return engine
    except Exception:
        app_state["project_healing"] = None
        return None

async def _init_project_healing(engine, project_id: str) -> None:
    """프로젝트 생성 시 healing_config 자동 부여 (베스트 에포트)."""
    if not engine:
        return
    db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
    if not db_url:
        return
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=10)
        try:
            await engine.on_project_created(project_id, conn)
        finally:
            await conn.close()
    except Exception:
        pass


class CreateProjectRequest(BaseModel):
    description: str
    mode: Optional[str] = "execution_only"  # "full_cycle" | "execution_only"


class CreateProjectResponse(BaseModel):
    project_id: str
    status: str
    checkpoint_stage: str
    interrupt_payload: dict | None = None



@router.get("/projects", summary="파이프라인 프로젝트 목록")
async def list_projects(limit: int = 20, offset: int = 0):
    """체크포인트 DB에서 pipeline 프로젝트 목록 반환."""
    from app.main import app_state
    import asyncpg
    import os

    db_url = os.getenv('DATABASE_URL', '').replace('postgresql://', 'postgres://')
    if not db_url:
        return {"projects": [], "total": 0}

    try:
        conn = await asyncpg.connect(db_url)
        rows = await conn.fetch(
            "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE 'project-%' ORDER BY thread_id LIMIT $1 OFFSET $2",
            limit, offset
        )
        total_row = await conn.fetchval("SELECT COUNT(DISTINCT thread_id) FROM checkpoints WHERE thread_id LIKE 'project-%'")
        await conn.close()
    except Exception:
        return {"projects": [], "total": 0}

    graph = app_state.get("graph")
    projects = []
    for row in rows:
        thread_id = row["thread_id"]
        project_id = thread_id.replace("project-", "", 1)
        proj = {"project_id": project_id, "thread_id": thread_id, "status": "unknown",
                "checkpoint_stage": None, "progress_percent": 0, "llm_calls": 0, "total_cost_usd": 0.0}
        if graph:
            try:
                state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
                if state and state.values:
                    v = state.values
                    stage = v.get("checkpoint_stage", "")
                    proj["checkpoint_stage"] = stage
                    proj["status"] = "checkpoint_pending" if stage == "interrupted" or state.next else stage or "completed"
                    proj["llm_calls"] = v.get("llm_calls_count", 0)
                    proj["total_cost_usd"] = v.get("total_cost_usd", 0.0)
                    STAGES = ["requirements", "design", "development", "testing", "judging", "deployment"]
                    proj["progress_percent"] = int((STAGES.index(stage) / len(STAGES)) * 100) if stage in STAGES else 0
            except Exception:
                pass
        projects.append(proj)

    return {"projects": projects, "total": int(total_row or 0)}

@router.post("/projects", response_model=CreateProjectResponse, summary="프로젝트 생성", description="PM 에이전트로 요구사항 분석 후 checkpoint_pending 상태 반환")
async def create_project(req: CreateProjectRequest):
    """프로젝트 생성 → PM 노드까지 실행 → interrupt에서 멈춤.

    mode="full_cycle"     → full_cycle_graph (ideation + execution)
    mode="execution_only" → 기존 8-agent graph (기본값, 하위 호환)
    """
    from app.main import app_state

    mode = req.mode or "execution_only"

    if mode == "full_cycle":
        from app.graphs.full_cycle_graph import build_full_cycle_graph
        graph = build_full_cycle_graph(checkpointer=app_state.get("checkpointer"))
    else:
        graph = app_state.get("graph")

    if not graph:
        raise HTTPException(503, "Graph not ready")

    project_id = str(uuid.uuid4())[:8]
    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    engine = await _get_healing_engine()
    await _init_project_healing(engine, project_id)

    if mode == "full_cycle":
        initial_state = {
            # Full-cycle 전용 필드
            "direction": req.description,
            "mode": mode,
            "project_id": project_id,
            "created_at": datetime.utcnow().isoformat(),
            "full_cycle_status": "ideation",
            # 기본값
            "llm_calls_count": 0,
            "total_cost_usd": 0.0,
            "cost_breakdown": {},
            "error_log": [],
        }
    else:
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
    if engine and engine.is_circuit_open(project_id):
        raise HTTPException(status_code=429, detail="Circuit breaker open for project")
    try:
        if engine:
            result = await engine.apply_l1_timer(project_id, "create_project", graph.ainvoke(initial_state, config=config))
        else:
            result = await graph.ainvoke(initial_state, config=config)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Project L1 timeout")

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


@router.get("/projects/{project_id}/status", summary="프로젝트 상태 조회", description="현재 체크포인트, 비용, 파일 등 프로젝트 전체 상태 반환")
async def get_project_status(project_id: str):
    """프로젝트 상태 상세 조회.

    응답: project_id, status, current_agent, progress_percent,
          checkpoints, costs, created_at, updated_at
    """
    from app.main import app_state
    from app.checkpoints import get_checkpoint_logs

    graph = app_state.get("graph")
    if not graph:
        raise HTTPException(503, "Graph not ready")

    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    state = await graph.aget_state(config)
    if not state or not state.values:
        raise HTTPException(404, "Project not found")

    stage = state.values.get("checkpoint_stage", "unknown")
    STAGE_PROGRESS = {
        "requirements": 10, "plan_review": 20, "design_review": 30,
        "development": 50, "midpoint_review": 60, "final_review": 80,
        "completed": 100, "cancelled": 0,
    }
    progress = STAGE_PROGRESS.get(stage, 50)

    checkpoint_logs = await get_checkpoint_logs(project_id)

    return {
        "project_id": project_id,
        "status": "completed" if stage == "completed" else "in_progress",
        "checkpoint_stage": stage,
        "current_agent": state.values.get("next_agent"),
        "progress_percent": progress,
        "checkpoints": checkpoint_logs,
        "costs": {
            "total_usd": state.values.get("total_cost_usd", 0.0),
            "llm_calls_count": state.values.get("llm_calls_count", 0),
            "by_agent": state.values.get("cost_breakdown", {}),
        },
        "generated_files_count": len(state.values.get("generated_files", [])),
        "created_at": state.values.get("created_at"),
        "error_log": state.values.get("error_log", []),
    }


@router.post("/projects/{project_id}/resume", summary="프로젝트 재개", description="인터럽트된 프로젝트를 승인/거절로 재개")
async def resume_project(project_id: str, approved: bool = True, feedback: str = "자동 승인"):
    """인터럽트된 프로젝트를 재개합니다 (승인/거절)."""
    from app.main import app_state

    graph = app_state.get("graph")
    if not graph:
        raise HTTPException(503, "Graph not ready")

    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    # interrupt를 resume — LangGraph Command 사용
    from langgraph.types import Command
    resume_value = True if approved else feedback
    engine = await _get_healing_engine()
    if engine and engine.is_circuit_open(project_id):
        raise HTTPException(status_code=429, detail="Circuit breaker open for project")
    try:
        if engine:
            result = await engine.apply_l1_timer(project_id, "resume_project", graph.ainvoke(Command(resume=resume_value), config=config))
        else:
            result = await graph.ainvoke(Command(resume=resume_value), config=config)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Project L1 timeout")

    interrupt_payload = None
    if "__interrupt__" in result:
        interrupts = result["__interrupt__"]
        if interrupts:
            interrupt_payload = {
                "value": interrupts[0].value,
                "id": str(interrupts[0].id) if hasattr(interrupts[0], "id") else None,
            }

    state = await graph.aget_state(config)
    stage = state.values.get("checkpoint_stage", "unknown") if state and state.values else "unknown"

    return {
        "project_id": project_id,
        "status": "completed" if stage == "completed" else "in_progress",
        "checkpoint_stage": stage,
        "interrupt_payload": interrupt_payload,
    }


@router.post("/projects/{project_id}/auto_run", summary="자동 실행", description="최대 10회 자동 승인으로 전체 파이프라인 완료")
async def auto_run_project(project_id: str):
    """프로젝트를 자동 승인 모드로 전체 실행합니다 (Phase 1.5 호환)."""
    from app.main import app_state

    graph = app_state.get("graph")
    if not graph:
        raise HTTPException(503, "Graph not ready")

    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    # 최대 10회 자동 승인 루프
    max_iterations = 10
    engine = await _get_healing_engine()
    for i in range(max_iterations):
        from langgraph.types import Command
        if engine and engine.is_circuit_open(project_id):
            raise HTTPException(status_code=429, detail="Circuit breaker open for project")
        try:
            if engine:
                result = await engine.apply_l1_timer(
                    project_id,
                    f"auto_run_{i + 1}",
                    graph.ainvoke(Command(resume=True), config=config),
                )
            else:
                result = await graph.ainvoke(Command(resume=True), config=config)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Project L1 timeout")
        state = await graph.aget_state(config)
        stage = state.values.get("checkpoint_stage", "unknown") if state and state.values else "unknown"

        if "__interrupt__" not in result or not result.get("__interrupt__"):
            # 더 이상 interrupt 없음 → 완료
            return {
                "project_id": project_id,
                "status": "completed",
                "checkpoint_stage": stage,
                "iterations": i + 1,
            }

    return {
        "project_id": project_id,
        "status": "max_iterations_reached",
        "checkpoint_stage": "unknown",
        "iterations": max_iterations,
    }
