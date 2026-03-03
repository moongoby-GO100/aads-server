"""
Supervisor: TaskSpec 검토 → 라우팅 결정 → 진행 상태 관리.
조건부 엣지 route_after_supervisor에 라우팅 위임.

역할:
- TaskSpec 유효성 검증
- 에이전트 라우팅 결정 (연구 필요 여부 판단 포함)
- 반복 횟수 및 비용 초과 감시
"""
import structlog
from app.graph.state import AADSState

logger = structlog.get_logger()

VALID_AGENTS = {"developer", "qa", "judge", "researcher", "architect"}
MAX_ITERATIONS = 5


async def supervisor_node(state: AADSState) -> dict:
    """
    1. TaskSpec 유효성 검증
    2. 연구 필요 여부 판단 (research_needed 플래그)
    3. 반복 횟수 초과 감시
    4. next_agent 결정
    라우팅 실행은 conditional edge(route_after_supervisor)가 담당.
    """
    logger.info("supervisor_node_start")

    task = state.get("current_task")
    if not task:
        logger.info("supervisor_no_task", action="end")
        return {"checkpoint_stage": "cancelled"}

    # TaskSpec 유효성 검증
    description = task.get("description", "")
    if not description or len(description.strip()) < 5:
        logger.warning("supervisor_invalid_taskspec", reason="description too short")
        return {
            "error_log": state.get("error_log", []) + ["TaskSpec description too short"],
            "checkpoint_stage": "cancelled",
        }

    # 반복 횟수 초과 감시
    iteration = state.get("iteration_count", 0)
    if iteration >= MAX_ITERATIONS:
        logger.error("supervisor_max_iterations", iteration=iteration)
        return {
            "error_log": state.get("error_log", []) + [f"Max iterations ({MAX_ITERATIONS}) reached"],
            "checkpoint_stage": "cancelled",
        }

    # 에이전트 결정
    assigned = task.get("assigned_agent", "developer")
    if assigned not in VALID_AGENTS:
        logger.warning("supervisor_unknown_agent", agent=assigned, fallback="developer")
        assigned = "developer"

    # 연구 필요 여부: 태스크에 research_needed 플래그 또는 제약에 "조사" 키워드
    constraints = task.get("constraints", [])
    research_needed = (
        task.get("research_needed", False)
        or any("조사" in c or "research" in c.lower() for c in constraints)
    )
    if research_needed and not state.get("research_results"):
        assigned = "researcher"
        logger.info("supervisor_research_required", original_agent=task.get("assigned_agent"))

    # 비용 초과 경고
    total_cost = state.get("total_cost_usd", 0.0)
    budget = task.get("budget_limit_usd", 10.0)
    if total_cost > budget * 0.8:
        logger.warning("supervisor_budget_warning", total_cost=total_cost, budget=budget)

    logger.info("supervisor_routing", target=assigned, iteration=iteration, research_needed=research_needed)
    return {
        "next_agent": assigned,
        "iteration_count": iteration + 1,
    }
