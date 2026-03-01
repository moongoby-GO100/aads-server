"""DAY 2 단위 테스트: State + ModelRouter + CostTracker"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def test_taskspec_serialize():
    from app.graph.state import TaskSpec
    spec = TaskSpec(
        description="투두 앱 만들어줘",
        assigned_agent="developer",
        success_criteria=["CRUD 기능 동작", "CLI 인터페이스 존재"],
    )
    data = spec.model_dump()
    assert data["description"] == "투두 앱 만들어줘"
    assert data["assigned_agent"] == "developer"
    assert len(data["success_criteria"]) == 2
    assert data["max_llm_calls"] == 15
    assert data["budget_limit_usd"] == 10.0
    assert data["status"] == "pending"
    # task_id auto-generated
    assert len(data["task_id"]) == 8


def test_taskspec_deserialize():
    from app.graph.state import TaskSpec
    data = {
        "description": "테스트",
        "assigned_agent": "developer",
        "success_criteria": ["테스트 통과"],
        "task_id": "abc12345",
        "status": "completed",
    }
    spec = TaskSpec(**data)
    assert spec.task_id == "abc12345"
    assert spec.status == "completed"


def test_cost_tracker_increment():
    from app.services.cost_tracker import check_and_increment

    class MockSettings:
        MAX_LLM_CALLS_PER_TASK = 15
        MAX_COST_PER_TASK_USD = 10.0
        COST_WARNING_THRESHOLD = 0.8

    state = {"llm_calls_count": 0, "total_cost_usd": 0.0, "cost_breakdown": {}}
    result = check_and_increment(state, 0.05, "pm", MockSettings())
    assert result["llm_calls_count"] == 1
    assert abs(result["total_cost_usd"] - 0.05) < 1e-6
    assert result["cost_breakdown"]["pm"] == 0.05


def test_cost_tracker_limit_exceeded():
    from app.services.cost_tracker import check_and_increment, CostLimitExceeded

    class MockSettings:
        MAX_LLM_CALLS_PER_TASK = 15
        MAX_COST_PER_TASK_USD = 10.0
        COST_WARNING_THRESHOLD = 0.8

    state = {"llm_calls_count": 15, "total_cost_usd": 0.0, "cost_breakdown": {}}
    with pytest.raises(CostLimitExceeded):
        check_and_increment(state, 0.01, "pm", MockSettings())


def test_estimate_cost():
    from app.services.model_router import estimate_cost, ModelConfig
    config = ModelConfig("anthropic", "claude-sonnet-4-6", 3.0, 15.0)
    cost = estimate_cost(config, 3000, 2000)
    # 3000/1M * 3.0 + 2000/1M * 15.0 = 0.009 + 0.03 = 0.039
    assert abs(cost - 0.039) < 1e-6


def test_model_router_agent_roles():
    from app.services.model_router import AGENT_MODELS
    required_roles = {"pm", "supervisor", "developer", "architect", "qa", "judge", "devops", "researcher"}
    assert required_roles.issubset(set(AGENT_MODELS.keys()))
    for role, models in AGENT_MODELS.items():
        assert "primary" in models
        assert "fallback" in models
