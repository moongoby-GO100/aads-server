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
        return "pm_requirements"  # 재수정 루프


def route_after_developer(state: AADSState) -> str:
    """Developer 이후: Week 1에서는 바로 종료. Week 2에서 QA→Judge 추가."""
    task = state.get("current_task", {})
    if task.get("status") == "completed":
        return "__end__"
    elif task.get("status") == "failed":
        # 재시도 가능한 경우 supervisor로 복귀
        iteration = state.get("iteration_count", 0)
        if iteration < 5:
            return "supervisor"
        return "__end__"
    return "__end__"
