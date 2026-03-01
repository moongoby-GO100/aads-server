"""
LLM 호출 카운터 + 비용 누적 추적.
R-012: 작업당 15회 한도.
설계서: 작업당 $10, 월 $500 한도.
"""
import structlog

logger = structlog.get_logger()


class CostLimitExceeded(Exception):
    """비용/호출 한도 초과."""
    pass


def check_and_increment(
    state: dict,
    cost_delta: float,
    agent_name: str,
    settings,
) -> dict:
    """
    호출 전 한도 체크, 통과 시 카운터 증가.
    Returns: 업데이트된 state 부분 dict.
    ⚠️ 이 함수는 노드 내에서 LLM 호출 전에 반드시 실행.
    """
    current_calls = state.get("llm_calls_count", 0)
    current_cost = state.get("total_cost_usd", 0.0)

    # R-012: 호출 횟수 한도
    if current_calls >= settings.MAX_LLM_CALLS_PER_TASK:
        raise CostLimitExceeded(
            f"LLM call limit exceeded: {current_calls}/{settings.MAX_LLM_CALLS_PER_TASK}"
        )

    # 비용 한도
    if current_cost + cost_delta > settings.MAX_COST_PER_TASK_USD:
        raise CostLimitExceeded(
            f"Task cost limit exceeded: ${current_cost + cost_delta:.2f}/${settings.MAX_COST_PER_TASK_USD}"
        )

    # 경고 (80% 도달시)
    new_cost = current_cost + cost_delta
    if new_cost > settings.MAX_COST_PER_TASK_USD * settings.COST_WARNING_THRESHOLD:
        logger.warning(
            "cost_warning",
            current=f"${new_cost:.2f}",
            limit=f"${settings.MAX_COST_PER_TASK_USD}",
        )

    # 에이전트별 비용 분류
    breakdown = dict(state.get("cost_breakdown", {}))
    breakdown[agent_name] = breakdown.get(agent_name, 0.0) + cost_delta

    return {
        "llm_calls_count": current_calls + 1,
        "total_cost_usd": new_cost,
        "cost_breakdown": breakdown,
    }
