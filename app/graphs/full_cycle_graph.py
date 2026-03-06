"""
AADS-128: Full-Cycle Graph — 서브그래프 A(아이디어→기획) + 서브그래프 B(8-Agent 실행) 통합.

흐름:
  START → ideation (IdeationState) → [map_plan_to_execution] → execution (AADSState) → END

mode 분기:
  mode="full_cycle"      → full_cycle_graph (ideation + execution)
  mode="execution_only"  → 기존 8-agent graph (하위 호환)
"""
from __future__ import annotations

import structlog
from typing import Optional
from typing_extensions import TypedDict

from app.graphs.ideation_subgraph import IdeationState

logger = structlog.get_logger()


# ─── FullCycleState ───────────────────────────────────────────────────────────

class FullCycleState(TypedDict, total=False):
    # ── 서브그래프 A (Ideation) 필드 ──
    direction: str
    budget: Optional[str]
    timeline: Optional[str]
    search_results: list
    strategy_report: Optional[dict]
    candidates: list
    ceo_decision_1: Optional[dict]
    ceo_decision_2: Optional[dict]
    selected_candidate: Optional[dict]
    prd: Optional[dict]
    architecture: Optional[dict]
    phase_plan: Optional[list]
    project_plan: Optional[dict]
    debate_round: int
    debate_history: list
    consensus_reached: bool
    task_specs: list
    ideation_status: str

    # ── 서브그래프 B (Execution / AADSState) 필드 ──
    messages: list
    current_task: Optional[dict]
    task_queue: list
    next_agent: Optional[str]
    active_agents: list
    checkpoint_stage: str
    approved_stages: list
    revision_count: int
    llm_calls_count: int
    total_cost_usd: float
    cost_breakdown: dict
    generated_files: list
    sandbox_results: list
    qa_test_results: list
    judge_verdict: Optional[dict]
    project_id: str
    created_at: str
    iteration_count: int
    error_log: list
    architect_design: Optional[dict]
    devops_result: Optional[dict]
    research_results: list

    # ── Full-Cycle 전용 필드 ──
    mode: str                     # "full_cycle" | "execution_only"
    full_cycle_status: str        # "ideation" | "execution" | "completed"


# ─── 상태 매핑 함수 ───────────────────────────────────────────────────────────

def map_plan_to_execution(state: FullCycleState) -> dict:
    """
    IdeationState 산출물 → AADSState 입력으로 변환.
      - task_specs[0] → current_task (첫 번째 TaskSpec)
      - task_specs[1:] → task_queue
      - project_plan → messages에 context로 첨부
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    task_specs = state.get("task_specs", [])
    project_plan = state.get("project_plan", {})
    selected_candidate = state.get("selected_candidate", {})

    # 첫 번째 TaskSpec → current_task
    current_task = None
    task_queue: list[dict] = []
    if task_specs:
        first = task_specs[0]
        current_task = {
            "task_id": first.get("task_id", "T0101"),
            "description": first.get("description", first.get("title", "")),
            "assigned_agent": "developer",
            "success_criteria": [first.get("title", "")],
            "constraints": [],
            "input_artifacts": [],
            "output_artifacts": [],
            "max_iterations": 5,
            "max_llm_calls": 15,
            "budget_limit_usd": 10.0,
            "status": "pending",
        }
        # 나머지 TaskSpec → task_queue (dict 형태)
        for ts in task_specs[1:]:
            task_queue.append({
                "task_id": ts.get("task_id", ""),
                "description": ts.get("description", ts.get("title", "")),
                "assigned_agent": "developer",
                "success_criteria": [ts.get("title", "")],
                "constraints": [],
                "input_artifacts": [],
                "output_artifacts": [],
                "max_iterations": 5,
                "max_llm_calls": 15,
                "budget_limit_usd": 10.0,
                "status": "pending",
            })

    # project_plan을 context 메시지로 변환
    plan_summary = ""
    if project_plan:
        candidate_title = selected_candidate.get("title", "Unknown")
        plan_summary = (
            f"프로젝트: {candidate_title}\n"
            f"PRD: {project_plan.get('prd', {}).get('project_name', '')}\n"
            f"아키텍처: {project_plan.get('architecture', {}).get('style', '')}\n"
        )
    direction = state.get("direction", "")
    human_content = direction or (current_task["description"] if current_task else "프로젝트 실행")
    if plan_summary:
        human_content = f"{human_content}\n\n[기획 컨텍스트]\n{plan_summary}"

    from datetime import datetime
    import uuid

    project_id = state.get("project_id", str(uuid.uuid4())[:8])

    mapped: dict = {
        "messages": [HumanMessage(content=human_content)],
        "current_task": current_task,
        "task_queue": task_queue,
        "next_agent": None,
        "active_agents": [],
        "checkpoint_stage": "requirements",
        "approved_stages": [],
        "revision_count": 0,
        "llm_calls_count": state.get("llm_calls_count", 0),
        "total_cost_usd": state.get("total_cost_usd", 0.0),
        "cost_breakdown": state.get("cost_breakdown", {}),
        "generated_files": [],
        "sandbox_results": [],
        "qa_test_results": [],
        "judge_verdict": None,
        "project_id": project_id,
        "created_at": state.get("created_at", datetime.now().isoformat()),
        "iteration_count": 0,
        "error_log": [],
        "architect_design": None,
        "devops_result": None,
        "research_results": [],
        "full_cycle_status": "execution",
    }

    logger.info(
        "map_plan_to_execution",
        task_specs_total=len(task_specs),
        task_queue_size=len(task_queue),
        current_task_id=current_task["task_id"] if current_task else None,
    )
    return mapped


# ─── 서브그래프 래퍼 노드 ─────────────────────────────────────────────────────

async def ideation_node(state: FullCycleState) -> dict:
    """
    서브그래프 A를 실행하고 FullCycleState에 결과를 병합.
    IdeationState 필드만 추출해 서브그래프에 전달.
    """
    from app.graphs.ideation_subgraph import build_ideation_subgraph

    logger.info("full_cycle_node_start", node="ideation")

    ideation_input: IdeationState = {
        "direction": state.get("direction", ""),
        "budget": state.get("budget"),
        "timeline": state.get("timeline"),
        "search_results": state.get("search_results", []),
        "strategy_report": state.get("strategy_report"),
        "candidates": state.get("candidates", []),
        "ceo_decision_1": state.get("ceo_decision_1"),
        "ceo_decision_2": state.get("ceo_decision_2"),
        "selected_candidate": state.get("selected_candidate"),
        "prd": state.get("prd"),
        "architecture": state.get("architecture"),
        "phase_plan": state.get("phase_plan"),
        "project_plan": state.get("project_plan"),
        "debate_round": state.get("debate_round", 0),
        "debate_history": state.get("debate_history", []),
        "consensus_reached": state.get("consensus_reached", False),
        "task_specs": state.get("task_specs", []),
        "status": state.get("ideation_status", ""),
    }

    # 서브그래프 실행 (MemorySaver로 독립 실행)
    subgraph = build_ideation_subgraph(checkpointer=None)
    result = await subgraph.ainvoke(ideation_input)

    logger.info(
        "full_cycle_node_done",
        node="ideation",
        task_specs=len(result.get("task_specs", [])),
        status=result.get("status"),
    )

    return {
        "search_results": result.get("search_results", []),
        "strategy_report": result.get("strategy_report"),
        "candidates": result.get("candidates", []),
        "ceo_decision_1": result.get("ceo_decision_1"),
        "ceo_decision_2": result.get("ceo_decision_2"),
        "selected_candidate": result.get("selected_candidate"),
        "prd": result.get("prd"),
        "architecture": result.get("architecture"),
        "phase_plan": result.get("phase_plan"),
        "project_plan": result.get("project_plan"),
        "debate_round": result.get("debate_round", 0),
        "debate_history": result.get("debate_history", []),
        "consensus_reached": result.get("consensus_reached", False),
        "task_specs": result.get("task_specs", []),
        "ideation_status": result.get("status", "completed"),
        "full_cycle_status": "mapping",
    }


async def execution_node(state: FullCycleState) -> dict:
    """
    서브그래프 B(8-agent)를 실행하고 FullCycleState에 결과를 병합.
    map_plan_to_execution으로 매핑한 뒤 기존 graph에 위임.
    """
    from app.graph.builder import compile_graph

    logger.info("full_cycle_node_start", node="execution")

    # 상태 매핑
    exec_input = map_plan_to_execution(state)

    # 8-agent 그래프 실행 (checkpointer 없이 독립 실행)
    exec_graph = await compile_graph(checkpointer=None)
    result = await exec_graph.ainvoke(exec_input)

    logger.info(
        "full_cycle_node_done",
        node="execution",
        checkpoint_stage=result.get("checkpoint_stage"),
        generated_files=len(result.get("generated_files", [])),
    )

    return {
        "messages": result.get("messages", []),
        "current_task": result.get("current_task"),
        "task_queue": result.get("task_queue", []),
        "next_agent": result.get("next_agent"),
        "active_agents": result.get("active_agents", []),
        "checkpoint_stage": result.get("checkpoint_stage", "completed"),
        "approved_stages": result.get("approved_stages", []),
        "revision_count": result.get("revision_count", 0),
        "llm_calls_count": result.get("llm_calls_count", 0),
        "total_cost_usd": result.get("total_cost_usd", 0.0),
        "cost_breakdown": result.get("cost_breakdown", {}),
        "generated_files": result.get("generated_files", []),
        "sandbox_results": result.get("sandbox_results", []),
        "qa_test_results": result.get("qa_test_results", []),
        "judge_verdict": result.get("judge_verdict"),
        "iteration_count": result.get("iteration_count", 0),
        "error_log": result.get("error_log", []),
        "architect_design": result.get("architect_design"),
        "devops_result": result.get("devops_result"),
        "research_results": result.get("research_results", []),
        "full_cycle_status": "completed",
    }


# ─── Full-Cycle 그래프 빌더 ───────────────────────────────────────────────────

def build_full_cycle_graph(checkpointer=None):
    """
    Full-Cycle 상위 그래프 컴파일.
    START → ideation → execution → END
    """
    from langgraph.graph import StateGraph, START, END

    builder = StateGraph(FullCycleState)

    # 노드 등록
    builder.add_node("ideation", ideation_node)
    builder.add_node("execution", execution_node)

    # 엣지: START → ideation → execution → END
    builder.add_edge(START, "ideation")
    builder.add_edge("ideation", "execution")
    builder.add_edge("execution", END)

    compile_kwargs: dict = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer

    graph = builder.compile(**compile_kwargs)
    logger.info("full_cycle_graph_compiled", nodes=["ideation", "execution"])
    return graph


async def compile_full_cycle_graph(checkpointer=None):
    """비동기 래퍼 — lifespan에서 호출 가능."""
    return build_full_cycle_graph(checkpointer=checkpointer)
