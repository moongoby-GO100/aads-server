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
    """Developer 이후: QA로 전달 (Week 2). 실패 시 supervisor 재시도."""
    task = state.get("current_task", {})
    if task.get("status") == "completed":
        return "qa"
    elif task.get("status") == "failed":
        iteration = state.get("iteration_count", 0)
        if iteration < 5:
            return "supervisor"
        return "__end__"
    # 기본: QA로 전달
    return "qa"


def route_after_qa(state: AADSState) -> str:
    """QA 이후: 항상 Judge로 전달 (Judge가 최종 판정)."""
    stage = state.get("checkpoint_stage", "final_review")
    if stage == "cancelled":
        return "__end__"
    return "judge"


def route_after_judge(state: AADSState) -> str:
    """Judge 이후: pass/conditional_pass → END, fail → Developer 재작업."""
    stage = state.get("checkpoint_stage", "completed")
    verdict_data = state.get("judge_verdict", {})
    verdict = verdict_data.get("verdict", "fail") if verdict_data else "fail"

    if stage == "completed":
        return "__end__"
    elif stage == "cancelled":
        return "__end__"
    elif stage == "development":
        # fail → Developer 재작업
        return "developer"
    else:
        return "__end__"
