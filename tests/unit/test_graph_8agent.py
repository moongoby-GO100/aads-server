"""Unit tests for 8-agent graph."""
import pytest
from unittest.mock import AsyncMock, patch


def test_8agent_graph_builds():
    """8-agent 그래프 빌드 성공 확인."""
    from app.graph.builder import build_aads_graph
    builder = build_aads_graph()
    assert builder is not None


def test_8agent_graph_nodes():
    """모든 8개 노드 등록 확인."""
    from app.graph.builder import build_aads_graph
    builder = build_aads_graph()
    # builder.nodes가 dict이므로 키 확인
    nodes = list(builder.nodes.keys())
    expected = ["pm_requirements", "supervisor", "architect", "developer", "qa", "judge", "devops", "researcher"]
    for node in expected:
        assert node in nodes, f"Node {node} not in graph"


def test_state_new_fields():
    """AADSState 새 필드 확인."""
    from app.graph.state import AADSState
    import typing
    hints = typing.get_type_hints(AADSState)
    assert "architect_design" in hints
    assert "devops_result" in hints
    assert "research_results" in hints
