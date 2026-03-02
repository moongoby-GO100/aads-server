"""
LLM 호출 카운터 + 비용 누적 추적.
R-012: 작업당 15회 한도.
설계서: 작업당 $10, 월 $500 한도.
Redis 카운터 연동 (Upstash Redis).
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

    # Redis 비동기 카운터 업데이트 (fire-and-forget, 실패 무시)
    project_id = state.get("project_id", "unknown")
    _try_redis_increment(project_id, agent_name, cost_delta, current_calls + 1)

    return {
        "llm_calls_count": current_calls + 1,
        "total_cost_usd": new_cost,
        "cost_breakdown": breakdown,
    }


def _try_redis_increment(project_id: str, agent_name: str, cost: float, call_count: int) -> None:
    """Redis 카운터 업데이트 (실패 시 무시 — graceful degradation)."""
    try:
        from app.config import settings
        redis_url = settings.UPSTASH_REDIS_URL
        if not redis_url:
            return

        import redis as redis_lib
        r = redis_lib.from_url(redis_url, decode_responses=True, socket_timeout=1)
        pipe = r.pipeline()
        pipe.hincrbyfloat(f"aads:project:{project_id}:costs", agent_name, cost)
        pipe.hset(f"aads:project:{project_id}:meta", "llm_calls", call_count)
        pipe.expire(f"aads:project:{project_id}:costs", 86400)
        pipe.expire(f"aads:project:{project_id}:meta", 86400)
        pipe.execute()
    except Exception:
        pass  # graceful degradation


async def get_project_costs(project_id: str, state_breakdown: dict) -> dict:
    """프로젝트 비용 상세 조회 (Redis + 상태 결합)."""
    redis_data = {}
    try:
        from app.config import settings
        redis_url = settings.UPSTASH_REDIS_URL
        if redis_url:
            import redis as redis_lib
            r = redis_lib.from_url(redis_url, decode_responses=True, socket_timeout=1)
            redis_data = r.hgetall(f"aads:project:{project_id}:costs") or {}
    except Exception:
        pass

    # 상태 breakdown과 Redis 데이터 결합 (상태 우선)
    merged = {k: float(v) for k, v in redis_data.items()}
    merged.update({k: float(v) for k, v in state_breakdown.items()})

    total = sum(merged.values())
    return {
        "project_id": project_id,
        "total_usd": round(total, 6),
        "by_agent": merged,
        "data_source": "redis+state" if redis_data else "state_only",
    }
