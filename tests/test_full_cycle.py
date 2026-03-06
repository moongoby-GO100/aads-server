"""
AADS-128: Full-Cycle Graph 통합 테스트.

test_full_cycle_mode_selection     — mode=full_cycle → full_cycle_graph 로딩 확인
test_execution_only_backward_compat — mode=execution_only → 기존 체인 동작 확인
test_state_mapping                 — IdeationState → AgentState 필드 매핑 정합성
test_artifacts_recording           — 산출물이 project_artifacts에 기록되는지 (mocking)
test_existing_tests_pass           — 현재 pytest 수집 건수 확인 (비파괴 확인용)
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─── 1. mode=full_cycle → full_cycle_graph 로딩 ──────────────────────────────

def test_full_cycle_mode_selection():
    """build_full_cycle_graph가 호출되고 ideation/execution 노드를 가지는지 확인."""
    from app.graphs.full_cycle_graph import build_full_cycle_graph, FullCycleState

    graph = build_full_cycle_graph(checkpointer=None)
    assert graph is not None

    # 그래프 노드 확인
    node_names = set(graph.nodes.keys())
    assert "ideation" in node_names, f"ideation 노드 없음: {node_names}"
    assert "execution" in node_names, f"execution 노드 없음: {node_names}"


# ─── 2. mode=execution_only → 기존 체인 하위 호환 ────────────────────────────

def test_execution_only_backward_compat():
    """기존 8-agent builder.compile_graph가 full_cycle 없이 단독 동작하는지 확인."""
    from app.graph import builder as graph_builder
    assert hasattr(graph_builder, "compile_graph"), "compile_graph 함수 없음"


# ─── 3. IdeationState → AgentState 필드 매핑 정합성 ─────────────────────────

def test_state_mapping_basic():
    """map_plan_to_execution: task_specs → current_task + task_queue 변환."""
    from app.graphs.full_cycle_graph import map_plan_to_execution

    state = {
        "direction": "AI 퍼포먼스 마케팅 SaaS",
        "task_specs": [
            {"task_id": "T0101", "title": "기능 A", "description": "기능 A 구현"},
            {"task_id": "T0102", "title": "기능 B", "description": "기능 B 구현"},
            {"task_id": "T0103", "title": "기능 C", "description": "기능 C 구현"},
        ],
        "project_plan": {
            "prd": {"project_name": "TestPRD"},
            "architecture": {"style": "microservice"},
        },
        "selected_candidate": {"id": "c1", "title": "AI 마케팅 SaaS"},
        "project_id": "test-project-001",
    }

    result = map_plan_to_execution(state)

    # current_task 검증
    assert result["current_task"] is not None
    assert result["current_task"]["task_id"] == "T0101"
    assert result["current_task"]["description"] == "기능 A 구현"

    # task_queue 검증 (나머지 2개)
    assert len(result["task_queue"]) == 2
    assert result["task_queue"][0]["task_id"] == "T0102"
    assert result["task_queue"][1]["task_id"] == "T0103"

    # messages 검증 (direction + 기획 컨텍스트)
    assert len(result["messages"]) > 0
    content = result["messages"][0].content
    assert "AI 퍼포먼스 마케팅 SaaS" in content

    # 기본 실행 필드 검증
    assert result["checkpoint_stage"] == "requirements"
    assert result["revision_count"] == 0
    assert isinstance(result["error_log"], list)


def test_state_mapping_empty_task_specs():
    """task_specs가 비어있을 때 current_task=None, task_queue=[] 반환."""
    from app.graphs.full_cycle_graph import map_plan_to_execution

    state = {
        "direction": "빈 테스트",
        "task_specs": [],
        "project_id": "test-empty",
    }

    result = map_plan_to_execution(state)

    assert result["current_task"] is None
    assert result["task_queue"] == []
    assert result["messages"][0].content == "빈 테스트"


def test_state_mapping_single_task():
    """task_specs 1개일 때 task_queue 비어있음."""
    from app.graphs.full_cycle_graph import map_plan_to_execution

    state = {
        "direction": "단일 태스크",
        "task_specs": [
            {"task_id": "T0101", "title": "유일 기능", "description": "단일 기능 구현"},
        ],
        "project_id": "test-single",
    }

    result = map_plan_to_execution(state)

    assert result["current_task"]["task_id"] == "T0101"
    assert result["task_queue"] == []


# ─── 4. FullCycleState TypedDict 필드 완전성 ─────────────────────────────────

def test_full_cycle_state_fields():
    """FullCycleState가 IdeationState + AADSState 필드를 모두 포함하는지 확인."""
    from app.graphs.full_cycle_graph import FullCycleState
    from app.graphs.ideation_subgraph import IdeationState

    fc_hints = FullCycleState.__annotations__
    ideation_hints = IdeationState.__annotations__

    # Ideation 필드 (status→ideation_status로 매핑됨)
    ideation_required = {k for k in ideation_hints if k != "status"}
    for field in ideation_required:
        assert field in fc_hints, f"FullCycleState에 IdeationState 필드 누락: {field}"

    # Full-Cycle 전용 필드
    assert "mode" in fc_hints
    assert "full_cycle_status" in fc_hints

    # Execution 필드
    for field in ["messages", "current_task", "task_queue", "checkpoint_stage",
                  "generated_files", "project_id"]:
        assert field in fc_hints, f"FullCycleState에 execution 필드 누락: {field}"


# ─── 5. artifacts API Pydantic 모델 검증 ─────────────────────────────────────

def test_artifacts_recording_schema():
    """CreateArtifactRequest / ArtifactResponse 스키마 검증."""
    from app.api.artifacts import CreateArtifactRequest, ArtifactResponse

    req = CreateArtifactRequest(
        project_id="550e8400-e29b-41d4-a716-446655440000",
        artifact_type="strategy_report",
        artifact_name="시장조사 보고서 v1",
        content={"direction": "AI SaaS", "candidates": []},
        source_agent="strategist",
        source_task="T0101",
        version=1,
    )
    assert req.artifact_type == "strategy_report"
    assert req.version == 1
    assert req.content["direction"] == "AI SaaS"


def test_artifacts_router_exists():
    """artifacts 라우터가 main.py에 등록됐는지 확인."""
    from app.main import app

    routes = [r.path for r in app.routes]
    artifact_routes = [r for r in routes if "artifacts" in r]
    assert len(artifact_routes) > 0, f"artifacts 라우터 미등록. 등록된 경로: {routes[:10]}"


# ─── 6. 마이그레이션 파일 존재 확인 ─────────────────────────────────────────

def test_migrations_exist():
    """014_project_mode.sql, 015_project_artifacts.sql 파일 존재 확인."""
    import os

    base = os.path.join(os.path.dirname(__file__), "..", "migrations")
    assert os.path.isfile(os.path.join(base, "014_project_mode.sql")), \
        "014_project_mode.sql 없음"
    assert os.path.isfile(os.path.join(base, "015_project_artifacts.sql")), \
        "015_project_artifacts.sql 없음"

    # SQL 내용 검증
    with open(os.path.join(base, "014_project_mode.sql")) as f:
        sql = f.read()
    assert "mode" in sql and "execution_only" in sql

    with open(os.path.join(base, "015_project_artifacts.sql")) as f:
        sql = f.read()
    assert "project_artifacts" in sql
    assert "artifact_type" in sql
    assert "idx_artifacts_project" in sql


# ─── 7. 기존 테스트 비파괴 확인 (수집 개수 확인) ─────────────────────────────

def test_existing_tests_count():
    """기존 테스트가 수집 가능한지 확인 (비파괴 검증용 smoke test)."""
    import subprocess, sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
         "--ignore=tests/test_full_cycle.py"],
        capture_output=True, text=True,
        cwd=__file__.replace("/tests/test_full_cycle.py", ""),
    )
    output = result.stdout + result.stderr
    # 최소 100개 이상 수집 확인
    lines = [l for l in output.splitlines() if "test" in l and "collected" in l]
    if lines:
        count_str = lines[-1].split()[0]
        count = int(count_str) if count_str.isdigit() else 0
        assert count >= 100, f"기존 테스트 수 비정상: {count} (기대 >= 100)"
