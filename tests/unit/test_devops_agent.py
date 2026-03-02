"""Unit tests for DevOps Agent."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_devops_node_basic():
    """기본 devops_node 실행 테스트."""
    from app.agents.devops_agent import devops_node

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(
        content='''```json
{
  "deploy_script": "uvicorn main:app --port 8080",
  "health_check_cmd": "echo ok",
  "env_vars": {"PORT": "8080"},
  "deploy_notes": "Python 3.11+"
}
```'''
    )

    with patch("app.agents.devops_agent.get_llm_for_agent",
               return_value=(mock_llm, MagicMock(input_cost_per_m=0.25, output_cost_per_m=2.0))), \
         patch("app.agents.devops_agent.estimate_cost", return_value=0.001), \
         patch("app.agents.devops_agent.check_and_increment",
               return_value={"llm_calls_count": 1, "total_cost_usd": 0.001, "cost_breakdown": {"devops": 0.001}}), \
         patch("app.agents.devops_agent.execute_in_sandbox",
               return_value={"exit_code": 0, "stdout": "ok", "stderr": ""}):

        state = {
            "current_task": {"description": "Deploy API", "success_criteria": []},
            "generated_files": [{"path": "main.py", "content": "app = FastAPI()"}],
            "judge_verdict": {"verdict": "pass", "score": 0.9},
            "llm_calls_count": 0,
            "total_cost_usd": 0.0,
            "cost_breakdown": {},
            "error_log": [],
        }
        result = await devops_node(state)

    assert "devops_result" in result
    assert result["checkpoint_stage"] == "completed"


@pytest.mark.asyncio
async def test_devops_node_skip_on_judge_fail():
    """Judge fail 시 devops 스킵."""
    from app.agents.devops_agent import devops_node

    state = {
        "current_task": {"description": "test"},
        "generated_files": [],
        "judge_verdict": {"verdict": "fail"},
        "llm_calls_count": 0,
        "total_cost_usd": 0.0,
        "cost_breakdown": {},
        "error_log": [],
    }
    result = await devops_node(state)
    assert result["checkpoint_stage"] == "completed"
    assert "devops_result" not in result
