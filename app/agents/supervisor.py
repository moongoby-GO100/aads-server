"""
Supervisor: TaskSpec 기반 동적 에이전트 배정 → 라우팅 결정 → 진행 상태 관리.
프로덕션 전환 (T-031): 병렬 실행, max_iterations CEO 에스컬레이션, fallback 체인.

역할:
- TaskSpec 유효성 검증
- assigned_agent 기반 동적 에이전트 배정
- 병렬 실행: 독립 태스크 동시 dispatch (Researcher + Architect)
- max_iterations 초과 → CEO 에스컬레이션
- max_llm_calls (R-012, 15회) 카운터 강화
- 에이전트 실패 시 fallback: 재시도 1회 → 다른 모델 → 에스컬레이션
"""
import asyncio
import structlog
from typing import Optional

from app.graph.state import AADSState
from app.services.ceo_notify import notify_ceo_escalation
from app.config import settings

logger = structlog.get_logger()

VALID_AGENTS = {"developer", "qa", "judge", "researcher", "architect", "devops"}
MAX_ITERATIONS = 5

# 병렬 실행 가능한 에이전트 쌍 (독립 태스크)
PARALLEL_AGENT_PAIRS = [
    {"researcher", "architect"},
]


async def supervisor_node(state: AADSState) -> dict:
    """
    프로덕션 Supervisor 노드:
    1. TaskSpec 유효성 검증
    2. max_llm_calls(R-012) 카운터 확인
    3. max_iterations 초과 시 CEO 에스컬레이션
    4. assigned_agent 기반 동적 배정
    5. 병렬 실행 가능 여부 판단
    6. 에이전트 실패 fallback 처리
    라우팅 실행은 conditional edge(route_after_supervisor)가 담당.
    """
    logger.info("supervisor_node_start_production")

    task = state.get("current_task")
    if not task:
        logger.info("supervisor_no_task", action="end")
        return {"checkpoint_stage": "cancelled"}

    # ── TaskSpec 유효성 검증 ──────────────────────────────────────────
    description = task.get("description", "")
    if not description or len(description.strip()) < 5:
        logger.warning("supervisor_invalid_taskspec", reason="description too short")
        return {
            "error_log": state.get("error_log", []) + ["TaskSpec description too short"],
            "checkpoint_stage": "cancelled",
        }

    # ── max_llm_calls 카운터 강화 (R-012) ───────────────────────────
    llm_calls = state.get("llm_calls_count", 0)
    if llm_calls >= settings.MAX_LLM_CALLS_PER_TASK:
        logger.error(
            "supervisor_llm_calls_limit",
            calls=llm_calls,
            limit=settings.MAX_LLM_CALLS_PER_TASK,
        )
        error_msg = f"LLM call limit exceeded: {llm_calls}/{settings.MAX_LLM_CALLS_PER_TASK}"
        # CEO 에스컬레이션
        await _escalate_to_ceo(state, error_msg)
        return {
            "error_log": state.get("error_log", []) + [error_msg],
            "checkpoint_stage": "cancelled",
            "next_agent": "ceo_escalation",
        }

    # ── 반복 횟수 초과 감시 → CEO 에스컬레이션 ──────────────────────
    iteration = state.get("iteration_count", 0)
    if iteration >= MAX_ITERATIONS:
        logger.error("supervisor_max_iterations", iteration=iteration)
        error_msg = f"Max iterations ({MAX_ITERATIONS}) reached — escalating to CEO"
        await _escalate_to_ceo(state, error_msg)
        return {
            "error_log": state.get("error_log", []) + [error_msg],
            "checkpoint_stage": "cancelled",
            "next_agent": "ceo_escalation",
        }

    # ── 에이전트 실패 fallback 처리 ──────────────────────────────────
    failure_agent = task.get("failed_agent")
    failure_count = task.get("failure_count", 0)
    if failure_agent:
        assigned = await _handle_agent_failure(
            state, task, failure_agent, failure_count
        )
        if assigned is None:
            # 에스컬레이션 결정됨
            return {
                "error_log": state.get("error_log", []) + [
                    f"Agent {failure_agent} failed {failure_count} times — escalating"
                ],
                "checkpoint_stage": "cancelled",
                "next_agent": "ceo_escalation",
            }
    else:
        # ── assigned_agent 기반 동적 배정 ────────────────────────────
        assigned = task.get("assigned_agent", "developer")
        if assigned not in VALID_AGENTS:
            logger.warning(
                "supervisor_unknown_agent", agent=assigned, fallback="developer"
            )
            assigned = "developer"

    # ── 연구 필요 여부 판단 ──────────────────────────────────────────
    constraints = task.get("constraints", [])
    research_needed = task.get("research_needed", False) or any(
        "조사" in c or "research" in c.lower() for c in constraints
    )
    if research_needed and not state.get("research_results"):
        logger.info(
            "supervisor_research_required", original_agent=task.get("assigned_agent")
        )
        # 병렬 가능 여부: Researcher + Architect 동시 실행 플래그 세팅
        parallel_agents = _check_parallel_execution(assigned, state)
        if parallel_agents:
            logger.info("supervisor_parallel_dispatch", agents=list(parallel_agents))
            return {
                "next_agent": "researcher",
                "parallel_agents": list(parallel_agents),
                "iteration_count": iteration + 1,
            }
        assigned = "researcher"

    # ── 비용 초과 경고 ────────────────────────────────────────────────
    total_cost = state.get("total_cost_usd", 0.0)
    budget = task.get("budget_limit_usd", settings.MAX_COST_PER_TASK_USD)
    if total_cost > budget * settings.COST_WARNING_THRESHOLD:
        logger.warning(
            "supervisor_budget_warning", total_cost=total_cost, budget=budget
        )

    logger.info(
        "supervisor_routing",
        target=assigned,
        iteration=iteration,
        research_needed=research_needed,
        llm_calls=llm_calls,
    )
    return {
        "next_agent": assigned,
        "iteration_count": iteration + 1,
    }


def _check_parallel_execution(primary_agent: str, state: AADSState) -> Optional[set]:
    """독립 태스크 병렬 실행 가능 여부 확인."""
    # Researcher + Architect 병렬 실행 조건:
    # architect_design이 없고 researcher가 필요한 경우
    if primary_agent in {"developer", "architect"}:
        if not state.get("architect_design") and not state.get("research_results"):
            return {"researcher", "architect"}
    return None


async def _handle_agent_failure(
    state: AADSState, task: dict, failed_agent: str, failure_count: int
) -> Optional[str]:
    """
    에이전트 실패 fallback 로직:
    1회 실패 → 동일 에이전트 재시도 (다른 모델)
    2회 이상 실패 → 에스컬레이션
    """
    logger.warning(
        "supervisor_agent_failure_fallback",
        failed_agent=failed_agent,
        failure_count=failure_count,
    )

    if failure_count == 1:
        # 재시도: 다른 모델로 교체 표시
        logger.info(
            "supervisor_fallback_retry",
            agent=failed_agent,
            action="switch_to_fallback_model",
        )
        # fallback 모델 사용 플래그 세팅 (get_llm_for_agent에서 use_fallback=True)
        task["use_fallback_model"] = True
        task["failure_count"] = failure_count + 1
        return failed_agent  # 동일 에이전트, fallback 모델로 재시도

    # 2회 이상 실패 → CEO 에스컬레이션
    error_msg = f"Agent {failed_agent} failed {failure_count} times after fallback"
    await _escalate_to_ceo(state, error_msg)
    return None


async def _escalate_to_ceo(state: AADSState, reason: str) -> None:
    """CEO 에스컬레이션 알림."""
    try:
        task = state.get("current_task", {})
        await notify_ceo_escalation(
            project_id=state.get("project_id", "unknown"),
            task_id=task.get("task_id", "unknown"),
            reason=reason,
            context={
                "iteration_count": state.get("iteration_count", 0),
                "llm_calls_count": state.get("llm_calls_count", 0),
                "total_cost_usd": state.get("total_cost_usd", 0.0),
                "description": task.get("description", "")[:200],
            },
        )
        logger.info("supervisor_ceo_escalation_sent", reason=reason)
    except Exception as e:
        logger.warning("supervisor_ceo_escalation_failed", error=str(e))
