"""5-agent 전체 흐름 테스트"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def test_graph_build():
    """그래프 빌드 성공 확인."""
    from app.graph.builder import build_aads_graph
    builder = build_aads_graph()
    assert builder is not None


def test_graph_nodes():
    """5개 노드 모두 등록 확인."""
    from app.graph.builder import build_aads_graph
    builder = build_aads_graph()
    # LangGraph StateGraph의 nodes는 딕셔너리
    nodes = builder.nodes
    assert "pm_requirements" in nodes
    assert "supervisor" in nodes
    assert "developer" in nodes
    assert "qa" in nodes
    assert "judge" in nodes


def test_routing_after_developer_completed():
    """Developer 완료 → QA 라우팅."""
    from app.graph.routing import route_after_developer
    state = {
        "current_task": {"status": "completed"},
        "iteration_count": 0,
        "checkpoint_stage": "development",
    }
    result = route_after_developer(state)
    assert result == "qa"


def test_routing_after_developer_failed():
    """Developer 실패 → supervisor 재시도 (iteration < 5)."""
    from app.graph.routing import route_after_developer
    state = {
        "current_task": {"status": "failed"},
        "iteration_count": 2,
        "checkpoint_stage": "development",
    }
    result = route_after_developer(state)
    assert result == "supervisor"


def test_routing_after_developer_max_iteration():
    """Developer 실패 + max iteration → END."""
    from app.graph.routing import route_after_developer
    state = {
        "current_task": {"status": "failed"},
        "iteration_count": 5,
        "checkpoint_stage": "development",
    }
    result = route_after_developer(state)
    assert result == "__end__"


def test_routing_after_qa():
    """QA 이후 → Judge 라우팅."""
    from app.graph.routing import route_after_qa
    state = {"checkpoint_stage": "final_review"}
    result = route_after_qa(state)
    assert result == "judge"


def test_routing_after_judge_pass():
    """Judge pass → END."""
    from app.graph.routing import route_after_judge
    state = {
        "checkpoint_stage": "completed",
        "judge_verdict": {"verdict": "pass", "score": 0.9, "issues": [], "recommendation": ""},
    }
    result = route_after_judge(state)
    assert result == "__end__"


def test_routing_after_judge_fail():
    """Judge fail + iteration < 3 → Developer 재작업."""
    from app.graph.routing import route_after_judge
    state = {
        "checkpoint_stage": "development",
        "judge_verdict": {"verdict": "fail", "score": 0.3, "issues": ["미충족"], "recommendation": ""},
        "iteration_count": 1,
    }
    result = route_after_judge(state)
    assert result == "developer"


def test_state_has_qa_judge_fields():
    """State에 qa_test_results, judge_verdict 필드 확인."""
    from app.graph.state import AADSState
    import typing
    hints = typing.get_type_hints(AADSState)
    assert "qa_test_results" in hints
    assert "judge_verdict" in hints
