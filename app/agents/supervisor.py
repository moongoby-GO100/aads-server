"""
Supervisor: TaskSpec의 assigned_agent를 확인하고 라우팅.
조건부 엣지 route_after_supervisor에 라우팅 위임.
"""
import structlog
from app.graph.state import AADSState

logger = structlog.get_logger()


async def supervisor_node(state: AADSState) -> dict:
    """
    현재 TaskSpec을 확인하고 next_agent 설정.
    라우팅은 conditional edge(route_after_supervisor)가 담당.
    """
    logger.info("supervisor_node_start")

    task = state.get("current_task")
    if not task:
        logger.info("supervisor_no_task", action="end")
        return {"checkpoint_stage": "cancelled"}

    assigned = task.get("assigned_agent", "developer")
    valid_agents = {"developer", "qa", "judge", "researcher"}
    if assigned not in valid_agents:
        logger.warning("supervisor_unknown_agent", agent=assigned, fallback="developer")
        assigned = "developer"

    iteration = state.get("iteration_count", 0)
    if iteration >= 5:
        logger.error("supervisor_max_iterations")
        return {
            "error_log": ["Max iterations (5) reached"],
            "checkpoint_stage": "cancelled",
        }

    logger.info("supervisor_routing", target=assigned, iteration=iteration)
    return {
        "next_agent": assigned,
        "iteration_count": iteration + 1,
    }
