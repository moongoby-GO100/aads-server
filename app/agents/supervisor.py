"""
Supervisor: TaskSpec의 assigned_agent를 확인하고 라우팅.
Week 1에서는 developer만 존재. Week 2에서 QA/Judge 추가.
"""
import structlog
from langgraph.types import Command

from app.graph.state import AADSState

logger = structlog.get_logger()


async def supervisor_node(state: AADSState) -> Command:
    """
    현재 TaskSpec을 확인하고 적절한 에이전트로 라우팅.
    Week 1: developer만 지원.
    """
    logger.info("supervisor_node_start")

    task = state.get("current_task")
    if not task:
        logger.info("supervisor_no_task", action="end")
        return Command(goto="__end__")

    assigned = task.get("assigned_agent", "developer")

    # 유효한 에이전트 확인 (Week 1: developer만)
    valid_agents = {"developer"}  # Week 2: + qa, judge

    if assigned not in valid_agents:
        logger.warning("supervisor_unknown_agent", agent=assigned, fallback="developer")
        assigned = "developer"

    # 반복 한도 체크
    iteration = state.get("iteration_count", 0)
    if iteration >= 5:
        logger.error("supervisor_max_iterations")
        return Command(
            goto="__end__",
            update={
                "error_log": state.get("error_log", []) + ["Max iterations (5) reached"],
                "checkpoint_stage": "cancelled",
            },
        )

    logger.info("supervisor_routing", target=assigned, iteration=iteration)
    return Command(
        goto=assigned,
        update={
            "next_agent": assigned,
            "iteration_count": iteration + 1,
        },
    )
