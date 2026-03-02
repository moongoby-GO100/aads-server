"""QA Agent 단위 테스트"""
import pytest
import sys
import os
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def make_state(**overrides):
    """기본 테스트 상태 생성."""
    base = {
        "messages": [],
        "current_task": {
            "task_id": "test001",
            "description": "두 수를 더하는 함수",
            "assigned_agent": "developer",
            "success_criteria": ["add(2, 3) == 5", "add(0, 0) == 0"],
            "constraints": [],
            "status": "completed",
        },
        "generated_files": [{
            "path": "main.py",
            "content": "def add(a, b):\n    return a + b\n",
            "language": "python",
        }],
        "sandbox_results": [],
        "qa_test_results": [],
        "judge_verdict": None,
        "llm_calls_count": 0,
        "total_cost_usd": 0.0,
        "cost_breakdown": {},
        "checkpoint_stage": "development",
        "iteration_count": 0,
        "error_log": [],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_qa_node_normal_flow():
    """QA Agent 정상 흐름: LLM + 샌드박스 성공."""
    from app.agents.qa_agent import qa_node

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(
        content='''테스트 전략: add 함수의 기본 동작을 검증합니다.
```python
def test_add_basic():
    assert add(2, 3) == 5

def test_add_zero():
    assert add(0, 0) == 0
```'''
    )

    mock_sandbox = AsyncMock(return_value={
        "stdout": "QA_RESULT: 2/2 passed\n",
        "stderr": "",
        "exit_code": 0,
    })

    with patch("app.agents.qa_agent.get_llm_for_agent", return_value=(mock_llm, MagicMock(input_cost_per_1m=3.0, output_cost_per_1m=15.0))), \
         patch("app.agents.qa_agent.estimate_cost", return_value=0.03), \
         patch("app.agents.qa_agent.check_and_increment", return_value={"llm_calls_count": 1, "total_cost_usd": 0.03, "cost_breakdown": {"qa": 0.03}}), \
         patch("app.agents.qa_agent.execute_in_sandbox", mock_sandbox):

        state = make_state()
        result = await qa_node(state)

    assert "qa_test_results" in result
    assert len(result["qa_test_results"]) == 1
    qa_result = result["qa_test_results"][0]
    assert qa_result["status"] == "pass"
    assert qa_result["tests_passed"] == 2
    assert qa_result["tests_total"] == 2
    assert result["checkpoint_stage"] == "final_review"


@pytest.mark.asyncio
async def test_qa_node_no_code():
    """Developer 코드 없을 때 skip 처리."""
    from app.agents.qa_agent import qa_node

    state = make_state(generated_files=[])
    result = await qa_node(state)

    assert "qa_test_results" in result
    assert result["qa_test_results"][0]["status"] == "skip"
    assert result["checkpoint_stage"] == "final_review"


@pytest.mark.asyncio
async def test_qa_node_sandbox_fail():
    """샌드박스 실패 시 fail 처리."""
    from app.agents.qa_agent import qa_node

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(
        content='''```python
def test_always_fail():
    assert False
```'''
    )

    mock_sandbox = AsyncMock(return_value={
        "stdout": "QA_RESULT: 0/1 passed\nFAIL: test_always_fail: assert False",
        "stderr": "",
        "exit_code": 1,
    })

    with patch("app.agents.qa_agent.get_llm_for_agent", return_value=(mock_llm, MagicMock(input_cost_per_1m=3.0, output_cost_per_1m=15.0))), \
         patch("app.agents.qa_agent.estimate_cost", return_value=0.03), \
         patch("app.agents.qa_agent.check_and_increment", return_value={"llm_calls_count": 1, "total_cost_usd": 0.03, "cost_breakdown": {"qa": 0.03}}), \
         patch("app.agents.qa_agent.execute_in_sandbox", mock_sandbox):

        state = make_state()
        result = await qa_node(state)

    assert result["qa_test_results"][0]["status"] == "fail"
    assert result["qa_test_results"][0]["tests_failed"] == 1
