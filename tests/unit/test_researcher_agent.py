"""Unit tests for Researcher Agent."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_researcher_node_basic():
    """기본 researcher_node 실행 테스트."""
    from app.agents.researcher_agent import researcher_node

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(
        content="- FastAPI recommended\n- SQLAlchemy for ORM"
    )

    with patch("app.agents.researcher_agent.get_llm_for_agent",
               return_value=(mock_llm, MagicMock(input_cost_per_m=0.3, output_cost_per_m=2.5))), \
         patch("app.agents.researcher_agent.estimate_cost", return_value=0.001), \
         patch("app.agents.researcher_agent.check_and_increment",
               return_value={"llm_calls_count": 1, "total_cost_usd": 0.001, "cost_breakdown": {"researcher": 0.001}}):

        state = {
            "current_task": {
                "description": "Build REST API",
                "success_criteria": ["CRUD operations"],
                "research_query": "Python REST API best practices",
            },
            "research_results": [],
            "llm_calls_count": 0,
            "total_cost_usd": 0.0,
            "cost_breakdown": {},
            "error_log": [],
        }
        result = await researcher_node(state)

    assert "research_results" in result
    assert len(result["research_results"]) == 1
    assert result["research_results"][0]["agent"] == "researcher"


@pytest.mark.asyncio
async def test_researcher_node_accumulates_results():
    """여러 번 호출 시 결과 누적."""
    from app.agents.researcher_agent import researcher_node

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(content="New findings")

    with patch("app.agents.researcher_agent.get_llm_for_agent",
               return_value=(mock_llm, MagicMock(input_cost_per_m=0.3, output_cost_per_m=2.5))), \
         patch("app.agents.researcher_agent.estimate_cost", return_value=0.001), \
         patch("app.agents.researcher_agent.check_and_increment",
               return_value={"llm_calls_count": 2, "total_cost_usd": 0.002, "cost_breakdown": {"researcher": 0.002}}):

        state = {
            "current_task": {"description": "test", "success_criteria": []},
            "research_results": [{"query": "prev", "findings": "prev findings", "agent": "researcher"}],
            "llm_calls_count": 1,
            "total_cost_usd": 0.001,
            "cost_breakdown": {},
            "error_log": [],
        }
        result = await researcher_node(state)

    assert len(result["research_results"]) == 2
