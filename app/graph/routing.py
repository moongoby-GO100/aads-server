"""조건부 에지 함수."""
from app.graph.state import AADSState


def route_after_pm(state: AADSState) -> str:
    """PM 이후 라우팅: 승인됐으면 supervisor, 취소면 종료, 아니면 다시 pm."""
    stage = state.get("checkpoint_stage", "requirements")
    if stage == "cancelled":
        return "__end__"
    elif stage == "plan_review":
        return "supervisor"
    else:
        return "pm_requirements"


def route_after_supervisor(state: AADSState) -> str:
    """Supervisor 이후: architect(설계 필요) or developer(설계 있음) or researcher."""
    stage = state.get("checkpoint_stage", "requirements")
    if stage == "cancelled":
        return "__end__"

    next_agent = state.get("next_agent", "")
    if next_agent == "researcher":
        return "researcher"

    # 설계 문서가 없으면 Architect 먼저
    architect_design = state.get("architect_design")
    if not architect_design:
        return "architect"

    return "developer"


def route_after_developer(state: AADSState) -> str:
    """Developer 이후: QA로 전달. 실패 시 supervisor 재시도."""
    task = state.get("current_task", {})
    if task.get("status") == "completed":
        return "qa"
    elif task.get("status") == "failed":
        iteration = state.get("iteration_count", 0)
        if iteration < 5:
            return "supervisor"
        return "__end__"
    return "qa"


def route_after_qa(state: AADSState) -> str:
    """QA 이후: 항상 Judge로 전달."""
    stage = state.get("checkpoint_stage", "final_review")
    if stage == "cancelled":
        return "__end__"
    return "judge"


def route_after_judge(state: AADSState) -> str:
    """Judge 이후: pass/conditional_pass → DevOps, fail → Developer 재작업."""
    stage = state.get("checkpoint_stage", "completed")
    verdict_data = state.get("judge_verdict", {})
    verdict = verdict_data.get("verdict", "fail") if verdict_data else "fail"

    if stage in ("completed", "cancelled"):
        return "__end__"
    elif stage == "development" and verdict == "fail":
        iteration = state.get("iteration_count", 0)
        if iteration < 3:
            return "developer"
        return "devops"
    else:
        return "devops"


def route_after_devops(state: AADSState) -> str:
    """DevOps 이후: 항상 종료."""
    return "__end__"
