"""
graph/routing.py 단위 테스트 — 커버리지 확대.
모든 조건부 엣지 함수의 분기를 검증.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from app.graph.routing import (
    route_after_pm,
    route_after_supervisor,
    route_after_developer,
    route_after_qa,
    route_after_judge,
    route_after_devops,
)


# ── route_after_pm ────────────────────────────────────────────────
class TestRouteAfterPm:
    def test_cancelled_returns_end(self):
        state = {"checkpoint_stage": "cancelled"}
        assert route_after_pm(state) == "__end__"

    def test_plan_review_returns_supervisor(self):
        state = {"checkpoint_stage": "plan_review"}
        assert route_after_pm(state) == "supervisor"

    def test_default_stage_returns_pm(self):
        state = {"checkpoint_stage": "requirements"}
        assert route_after_pm(state) == "pm_requirements"

    def test_unknown_stage_returns_pm(self):
        state = {"checkpoint_stage": "unknown"}
        assert route_after_pm(state) == "pm_requirements"

    def test_missing_stage_defaults_to_pm(self):
        state = {}
        assert route_after_pm(state) == "pm_requirements"


# ── route_after_supervisor ────────────────────────────────────────
class TestRouteAfterSupervisor:
    def test_cancelled_returns_end(self):
        state = {"checkpoint_stage": "cancelled"}
        assert route_after_supervisor(state) == "__end__"

    def test_researcher_agent_returns_researcher(self):
        state = {
            "checkpoint_stage": "plan_review",
            "next_agent": "researcher",
        }
        assert route_after_supervisor(state) == "researcher"

    def test_no_architect_design_goes_to_architect(self):
        state = {
            "checkpoint_stage": "plan_review",
            "next_agent": "developer",
            "architect_design": None,
        }
        assert route_after_supervisor(state) == "architect"

    def test_with_architect_design_goes_to_developer(self):
        state = {
            "checkpoint_stage": "plan_review",
            "next_agent": "developer",
            "architect_design": {"tech_stack": ["python"]},
        }
        assert route_after_supervisor(state) == "developer"

    def test_no_next_agent_no_design_goes_to_architect(self):
        state = {"checkpoint_stage": "plan_review"}
        assert route_after_supervisor(state) == "architect"


# ── route_after_developer ─────────────────────────────────────────
class TestRouteAfterDeveloper:
    def test_completed_task_goes_to_qa(self):
        state = {"current_task": {"status": "completed"}}
        assert route_after_developer(state) == "qa"

    def test_failed_task_low_iteration_goes_to_supervisor(self):
        state = {"current_task": {"status": "failed"}, "iteration_count": 2}
        assert route_after_developer(state) == "supervisor"

    def test_failed_task_max_iteration_ends(self):
        state = {"current_task": {"status": "failed"}, "iteration_count": 5}
        assert route_after_developer(state) == "__end__"

    def test_other_status_defaults_to_qa(self):
        state = {"current_task": {"status": "pending"}}
        assert route_after_developer(state) == "qa"

    def test_empty_task_defaults_to_qa(self):
        state = {"current_task": {}}
        assert route_after_developer(state) == "qa"


# ── route_after_qa ────────────────────────────────────────────────
class TestRouteAfterQa:
    def test_normal_goes_to_judge(self):
        state = {"checkpoint_stage": "final_review"}
        assert route_after_qa(state) == "judge"

    def test_cancelled_returns_end(self):
        state = {"checkpoint_stage": "cancelled"}
        assert route_after_qa(state) == "__end__"

    def test_any_stage_goes_to_judge(self):
        state = {"checkpoint_stage": "midpoint_review"}
        assert route_after_qa(state) == "judge"


# ── route_after_judge ─────────────────────────────────────────────
class TestRouteAfterJudge:
    def test_completed_stage_ends(self):
        state = {"checkpoint_stage": "completed", "judge_verdict": {"verdict": "pass"}}
        assert route_after_judge(state) == "__end__"

    def test_cancelled_stage_ends(self):
        state = {"checkpoint_stage": "cancelled", "judge_verdict": {}}
        assert route_after_judge(state) == "__end__"

    def test_fail_verdict_low_iteration_retries_developer(self):
        state = {
            "checkpoint_stage": "development",
            "judge_verdict": {"verdict": "fail"},
            "iteration_count": 1,
        }
        assert route_after_judge(state) == "developer"

    def test_fail_verdict_max_iteration_goes_to_devops(self):
        state = {
            "checkpoint_stage": "development",
            "judge_verdict": {"verdict": "fail"},
            "iteration_count": 3,
        }
        assert route_after_judge(state) == "devops"

    def test_no_verdict_fallback_to_devops(self):
        state = {"checkpoint_stage": "something_else", "judge_verdict": {}}
        assert route_after_judge(state) == "devops"

    def test_no_judge_verdict_key(self):
        state = {"checkpoint_stage": "something_else"}
        assert route_after_judge(state) == "devops"


# ── route_after_devops ────────────────────────────────────────────
class TestRouteAfterDevops:
    def test_always_returns_end(self):
        assert route_after_devops({}) == "__end__"
        assert route_after_devops({"anything": "value"}) == "__end__"
