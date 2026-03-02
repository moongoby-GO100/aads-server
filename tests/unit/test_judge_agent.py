"""Judge Agent 단위 테스트"""
import pytest
import sys
import os
import json
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def make_state(**overrides):
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
        "sandbox_results": [{"stdout": "5\n0", "stderr": "", "exit_code": 0}],
        "qa_test_results": [{"status": "pass", "tests_passed": 2, "tests_failed": 0, "tests_total": 2}],
        "judge_verdict": None,
        "llm_calls_count": 2,
        "total_cost_usd": 0.06,
        "cost_breakdown": {},
        "checkpoint_stage": "final_review",
        "iteration_count": 0,
        "error_log": [],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_judge_node_pass():
    """Judge Agent: pass 판정."""
    from app.agents.judge_agent import judge_node

    verdict = {"verdict": "pass", "score": 0.95, "issues": [], "recommendation": "기준 모두 충족"}

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(verdict))

    with patch("app.agents.judge_agent.get_llm_for_agent", return_value=(mock_llm, MagicMock(input_cost_per_1m=3.0, output_cost_per_1m=15.0))), \
         patch("app.agents.judge_agent.estimate_cost", return_value=0.02), \
         patch("app.agents.judge_agent.check_and_increment", return_value={"llm_calls_count": 3, "total_cost_usd": 0.08, "cost_breakdown": {"judge": 0.02}}):

        state = make_state()
        result = await judge_node(state)

    assert result["judge_verdict"]["verdict"] == "pass"
    assert result["judge_verdict"]["score"] == 0.95
    assert result["checkpoint_stage"] == "completed"


@pytest.mark.asyncio
async def test_judge_node_fail_retry():
    """Judge Agent: fail → Developer 재작업 (iteration < 3)."""
    from app.agents.judge_agent import judge_node

    verdict = {"verdict": "fail", "score": 0.3, "issues": ["add 함수 없음"], "recommendation": "add 함수 구현 필요"}

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(verdict))

    with patch("app.agents.judge_agent.get_llm_for_agent", return_value=(mock_llm, MagicMock(input_cost_per_1m=3.0, output_cost_per_1m=15.0))), \
         patch("app.agents.judge_agent.estimate_cost", return_value=0.02), \
         patch("app.agents.judge_agent.check_and_increment", return_value={"llm_calls_count": 3, "total_cost_usd": 0.08, "cost_breakdown": {"judge": 0.02}}):

        state = make_state(iteration_count=0)
        result = await judge_node(state)

    assert result["judge_verdict"]["verdict"] == "fail"
    assert result["checkpoint_stage"] == "development"  # 재작업
    assert result["iteration_count"] == 1


@pytest.mark.asyncio
async def test_judge_node_fail_max_retries():
    """Judge Agent: fail이지만 최대 재작업 횟수(3) 초과 → completed."""
    from app.agents.judge_agent import judge_node

    verdict = {"verdict": "fail", "score": 0.2, "issues": ["여전히 미충족"], "recommendation": "재작업 필요"}

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(verdict))

    with patch("app.agents.judge_agent.get_llm_for_agent", return_value=(mock_llm, MagicMock(input_cost_per_1m=3.0, output_cost_per_1m=15.0))), \
         patch("app.agents.judge_agent.estimate_cost", return_value=0.02), \
         patch("app.agents.judge_agent.check_and_increment", return_value={"llm_calls_count": 3, "total_cost_usd": 0.08, "cost_breakdown": {"judge": 0.02}}):

        state = make_state(iteration_count=3)  # 이미 3회 재작업
        result = await judge_node(state)

    assert result["checkpoint_stage"] == "completed"  # 강제 완료


@pytest.mark.asyncio
async def test_judge_node_conditional_pass():
    """Judge Agent: conditional_pass → completed."""
    from app.agents.judge_agent import judge_node

    verdict = {
        "verdict": "conditional_pass",
        "score": 0.75,
        "issues": ["에러 처리 미흡"],
        "recommendation": "기본 기능은 동작하나 에러 처리 보완 필요",
    }

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(verdict))

    with patch("app.agents.judge_agent.get_llm_for_agent", return_value=(mock_llm, MagicMock(input_cost_per_1m=3.0, output_cost_per_1m=15.0))), \
         patch("app.agents.judge_agent.estimate_cost", return_value=0.02), \
         patch("app.agents.judge_agent.check_and_increment", return_value={"llm_calls_count": 3, "total_cost_usd": 0.08, "cost_breakdown": {"judge": 0.02}}):

        state = make_state()
        result = await judge_node(state)

    assert result["judge_verdict"]["verdict"] == "conditional_pass"
    assert result["checkpoint_stage"] == "completed"


@pytest.mark.asyncio
async def test_judge_node_invalid_json():
    """Judge Agent: JSON 파싱 실패 시 conditional_pass 기본값."""
    from app.agents.judge_agent import judge_node

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = MagicMock(content="판정 결과를 텍스트로 드립니다: 통과입니다.")

    with patch("app.agents.judge_agent.get_llm_for_agent", return_value=(mock_llm, MagicMock(input_cost_per_1m=3.0, output_cost_per_1m=15.0))), \
         patch("app.agents.judge_agent.estimate_cost", return_value=0.02), \
         patch("app.agents.judge_agent.check_and_increment", return_value={"llm_calls_count": 3, "total_cost_usd": 0.08, "cost_breakdown": {"judge": 0.02}}):

        state = make_state()
        result = await judge_node(state)

    assert result["judge_verdict"]["verdict"] == "conditional_pass"
