"""Unit tests for Architect Agent."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_architect_node_basic():
    """기본 architect_node 실행 테스트."""
    from app.agents.architect_agent import architect_node

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(
        content='''```json
{
  "db_schema": "users table: id, name, email",
  "api_structure": "GET /users, POST /users",
  "file_structure": "src/main.py",
  "tech_stack": ["python", "fastapi"],
  "implementation_notes": "Use SQLite"
}
```'''
    )

    with patch("app.agents.architect_agent.get_llm_for_agent",
               return_value=(mock_llm, MagicMock(input_cost_per_m=3.0, output_cost_per_m=15.0))), \
         patch("app.agents.architect_agent.estimate_cost", return_value=0.01), \
         patch("app.agents.architect_agent.check_and_increment",
               return_value={"llm_calls_count": 1, "total_cost_usd": 0.01, "cost_breakdown": {"architect": 0.01}}):

        state = {
            "current_task": {
                "description": "Create user management API",
                "success_criteria": ["CRUD endpoints"],
                "constraints": [],
            },
            "llm_calls_count": 0,
            "total_cost_usd": 0.0,
            "cost_breakdown": {},
            "error_log": [],
        }
        result = await architect_node(state)

    assert "architect_design" in result
    assert result["checkpoint_stage"] == "development"
    design = result["architect_design"]
    assert "tech_stack" in design


@pytest.mark.asyncio
async def test_architect_node_cost_limit():
    """비용 한도 초과 시 cancelled 반환."""
    from app.agents.architect_agent import architect_node
    from app.services.cost_tracker import CostLimitExceeded

    with patch("app.agents.architect_agent.get_llm_for_agent",
               return_value=(AsyncMock(), MagicMock(input_cost_per_m=3.0, output_cost_per_m=15.0))), \
         patch("app.agents.architect_agent.estimate_cost", return_value=0.01), \
         patch("app.agents.architect_agent.check_and_increment",
               side_effect=CostLimitExceeded("Cost limit")):

        state = {
            "current_task": {"description": "test", "success_criteria": [], "constraints": []},
            "llm_calls_count": 15,
            "total_cost_usd": 10.0,
            "cost_breakdown": {},
            "error_log": [],
        }
        result = await architect_node(state)

    assert result["checkpoint_stage"] == "cancelled"
    assert len(result["error_log"]) > 0
