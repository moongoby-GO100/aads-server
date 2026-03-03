"""
supervisor.py 단위 테스트 — 커버리지 확대.
Supervisor 노드의 모든 분기 검증.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.mark.asyncio
async def test_supervisor_no_task_returns_cancelled():
    """TaskSpec 없으면 cancelled 반환."""
    from app.agents.supervisor import supervisor_node
    state = {"messages": [], "current_task": None}
    result = await supervisor_node(state)
    assert result["checkpoint_stage"] == "cancelled"


@pytest.mark.asyncio
async def test_supervisor_empty_description_cancelled():
    """description이 너무 짧으면 cancelled."""
    from app.agents.supervisor import supervisor_node
    state = {
        "current_task": {"description": "hi", "assigned_agent": "developer"},
        "iteration_count": 0,
    }
    result = await supervisor_node(state)
    assert result["checkpoint_stage"] == "cancelled"


@pytest.mark.asyncio
async def test_supervisor_valid_task_routes_developer():
    """정상 TaskSpec → developer 라우팅."""
    from app.agents.supervisor import supervisor_node
    state = {
        "current_task": {
            "description": "투두 앱을 만들어주세요. 기본 CRUD 기능 포함.",
            "assigned_agent": "developer",
            "constraints": [],
        },
        "iteration_count": 0,
    }
    result = await supervisor_node(state)
    assert result["next_agent"] == "developer"
    assert result["iteration_count"] == 1


@pytest.mark.asyncio
async def test_supervisor_max_iterations_cancelled():
    """반복 5회 초과 시 cancelled."""
    from app.agents.supervisor import supervisor_node
    state = {
        "current_task": {
            "description": "투두 앱을 만들어주세요. 기본 CRUD 기능 포함.",
            "assigned_agent": "developer",
        },
        "iteration_count": 5,
    }
    result = await supervisor_node(state)
    assert result["checkpoint_stage"] == "cancelled"
    assert "Max iterations" in result["error_log"][-1]


@pytest.mark.asyncio
async def test_supervisor_research_needed_routes_researcher():
    """research_needed=True이면 researcher 먼저."""
    from app.agents.supervisor import supervisor_node
    state = {
        "current_task": {
            "description": "최신 AI 라이브러리를 활용한 텍스트 분류기를 만들어주세요.",
            "assigned_agent": "developer",
            "research_needed": True,
            "constraints": [],
        },
        "iteration_count": 0,
        "research_results": [],
    }
    result = await supervisor_node(state)
    assert result["next_agent"] == "researcher"


@pytest.mark.asyncio
async def test_supervisor_research_from_constraints():
    """constraints에 '조사' 키워드 → researcher 라우팅."""
    from app.agents.supervisor import supervisor_node
    state = {
        "current_task": {
            "description": "데이터 파이프라인을 구현해주세요. 최신 라이브러리 조사 후 선택.",
            "assigned_agent": "developer",
            "constraints": ["라이브러리 조사 후 선택"],
        },
        "iteration_count": 0,
        "research_results": [],
    }
    result = await supervisor_node(state)
    assert result["next_agent"] == "researcher"


@pytest.mark.asyncio
async def test_supervisor_skip_research_if_results_exist():
    """이미 research_results 있으면 researcher 스킵."""
    from app.agents.supervisor import supervisor_node
    state = {
        "current_task": {
            "description": "데이터 파이프라인을 구현해주세요. 최신 라이브러리 조사 후 선택.",
            "assigned_agent": "developer",
            "research_needed": True,
            "constraints": [],
        },
        "iteration_count": 0,
        "research_results": [{"findings": "pandas 사용 권장"}],
    }
    result = await supervisor_node(state)
    assert result["next_agent"] == "developer"


@pytest.mark.asyncio
async def test_supervisor_unknown_agent_fallback_developer():
    """알 수 없는 에이전트 이름 → developer로 fallback."""
    from app.agents.supervisor import supervisor_node
    state = {
        "current_task": {
            "description": "프로그램을 작성해주세요. 기능 테스트도 포함해야 합니다.",
            "assigned_agent": "unknown_agent",
            "constraints": [],
        },
        "iteration_count": 0,
    }
    result = await supervisor_node(state)
    assert result["next_agent"] == "developer"
