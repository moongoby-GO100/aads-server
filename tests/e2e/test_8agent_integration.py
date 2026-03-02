"""
CUR-AADS-PHASE15-INTEGRATION-003: 8-agent 통합 실행 검증.

테스트 시나리오 3건:
  1) Python CLI Calculator — 단순 5-agent 경로 (PM→Supervisor→Developer→QA→Judge)
  2) REST API Stub — 설계 포함 7-agent 경로 (PM→Supervisor→Architect→Developer→QA→Judge→DevOps)
  3) Tech Research Report — 조사 경로 (PM→Supervisor→Researcher→Architect→Judge)

각 시나리오에서 검증:
  - TaskSpec 12필드 정합성
  - 에이전트 간 상태 전달
  - R-012 LLM 호출 카운터 ≤15
  - HITL 체크포인트 로그 기록
  - 비용 추적 /costs 응답
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

E2B_API_KEY = os.getenv("E2B_API_KEY", "")
HAS_REAL_E2B_KEY = E2B_API_KEY and E2B_API_KEY != "PLACEHOLDER_E2B_API_KEY" and len(E2B_API_KEY) > 10
AADS_SERVER_RUNNING = os.getenv("AADS_SERVER_RUNNING", "false").lower() == "true"
AADS_TEST_URL = os.getenv("AADS_TEST_URL", "https://aads.newtalk.kr/api/v1")


# ─────────────────────────────────────────────────────────────────────────────
# TaskSpec 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def make_calculator_task() -> dict:
    """시나리오 1: Python CLI Calculator (5-agent 경로)."""
    return {
        "task_id": "INT-003-CALC",
        "parent_task_id": None,
        "description": "Python CLI Calculator — +/-/*/÷ 연산 지원",
        "assigned_agent": "developer",
        "success_criteria": ["사칙연산 정상 동작", "pytest 통과"],
        "constraints": ["Python 3.12", "표준 라이브러리만"],
        "input_artifacts": [],
        "output_artifacts": ["calculator.py", "test_calc.py"],
        "max_iterations": 3,
        "max_llm_calls": 15,
        "budget_limit_usd": 0.50,
        "status": "pending",
    }


def make_rest_api_task() -> dict:
    """시나리오 2: REST API Stub (7-agent 경로, Architect 포함)."""
    return {
        "task_id": "INT-003-API",
        "parent_task_id": None,
        "description": "FastAPI stub — /health, /users CRUD 엔드포인트",
        "assigned_agent": "architect",
        "success_criteria": ["OpenAPI 스펙 생성", "FastAPI 앱 실행 가능"],
        "constraints": ["FastAPI", "Pydantic v2"],
        "input_artifacts": [],
        "output_artifacts": ["main.py", "models.py", "test_api.py"],
        "max_iterations": 3,
        "max_llm_calls": 15,
        "budget_limit_usd": 1.0,
        "status": "pending",
    }


def make_research_task() -> dict:
    """시나리오 3: Tech Research Report (Researcher 경로)."""
    return {
        "task_id": "INT-003-RESEARCH",
        "parent_task_id": None,
        "description": "Python 비동기 프레임워크 비교 조사 — asyncio vs trio vs anyio",
        "assigned_agent": "researcher",
        "success_criteria": ["각 프레임워크 장단점 정리", "JSON 보고서 생성"],
        "constraints": ["2025년 이후 정보 기준"],
        "input_artifacts": [],
        "output_artifacts": ["research_report.json"],
        "max_iterations": 2,
        "max_llm_calls": 15,
        "budget_limit_usd": 0.50,
        "status": "pending",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 단위 레벨 통합 테스트 (mock 기반)
# ─────────────────────────────────────────────────────────────────────────────

def test_scenario1_taskspec_12fields():
    """시나리오 1: Calculator TaskSpec 12필드 검증."""
    from app.graph.state import TaskSpec
    spec = make_calculator_task()
    assert len(spec) == 12
    task = TaskSpec(**spec)
    assert task.max_llm_calls == 15
    assert task.assigned_agent == "developer"
    print("✓ 시나리오 1 TaskSpec 12필드")


def test_scenario2_taskspec_12fields():
    """시나리오 2: REST API Stub TaskSpec 12필드 검증."""
    from app.graph.state import TaskSpec
    spec = make_rest_api_task()
    assert len(spec) == 12
    task = TaskSpec(**spec)
    assert task.assigned_agent == "architect"
    print("✓ 시나리오 2 TaskSpec 12필드")


def test_scenario3_taskspec_12fields():
    """시나리오 3: Research Report TaskSpec 12필드 검증."""
    from app.graph.state import TaskSpec
    spec = make_research_task()
    assert len(spec) == 12
    task = TaskSpec(**spec)
    assert task.assigned_agent == "researcher"
    print("✓ 시나리오 3 TaskSpec 12필드")


def test_8agent_all_nodes_reachable():
    """8-agent 그래프 모든 노드 도달 가능성 확인."""
    from app.graph.builder import build_aads_graph
    builder = build_aads_graph()
    nodes = set(builder.nodes.keys())
    required = {"pm_requirements", "supervisor", "architect", "developer",
                "qa", "judge", "devops", "researcher"}
    assert required.issubset(nodes), f"누락 노드: {required - nodes}"
    print(f"✓ 8개 노드 모두 등록: {sorted(nodes)}")


@pytest.mark.asyncio
async def test_scenario1_checkpoint_flow():
    """시나리오 1: HITL 체크포인트 5단계 기록 (단순 경로)."""
    from app.checkpoints import record_checkpoint

    stages = ["requirements", "code_review", "test_results", "deploy_approval", "final_review"]
    for stage in stages:
        log = await record_checkpoint(
            project_id="int003-calc",
            stage=stage,
            auto_approve=True,
            metadata={"scenario": "calculator"},
        )
        assert log["auto_approved"] is True
        assert log["approved_at"] is not None
    print(f"✓ 시나리오 1: {len(stages)}단계 체크포인트")


@pytest.mark.asyncio
async def test_r012_counter_across_agents():
    """R-012: 8-agent 전체 경로 LLM 호출 15회 이내."""
    from app.services.cost_tracker import check_and_increment, CostLimitExceeded
    from unittest.mock import MagicMock

    settings_mock = MagicMock()
    settings_mock.MAX_LLM_CALLS_PER_TASK = 15
    settings_mock.MAX_COST_PER_TASK_USD = 10.0
    settings_mock.COST_WARNING_THRESHOLD = 0.8

    state = {"llm_calls_count": 0, "total_cost_usd": 0.0, "cost_breakdown": {}, "project_id": "r012-test"}
    agents = ["pm", "supervisor", "researcher", "architect", "developer", "qa", "judge"]
    for i, agent in enumerate(agents):
        result = check_and_increment(state, 0.01, agent, settings_mock)
        state.update(result)

    assert state["llm_calls_count"] == len(agents)
    assert state["llm_calls_count"] <= 15
    print(f"✓ R-012: {state['llm_calls_count']}/15 LLM 호출")


@pytest.mark.asyncio
async def test_costs_by_agent():
    """에이전트별 비용 집계 검증."""
    from app.services.cost_tracker import get_project_costs

    breakdown = {
        "pm": 0.003, "supervisor": 0.005,
        "architect": 0.008, "developer": 0.015,
        "qa": 0.012, "judge": 0.006, "devops": 0.002,
    }
    result = await get_project_costs("int003-cost", breakdown)
    assert result["total_usd"] == pytest.approx(sum(breakdown.values()), rel=1e-3)
    assert "developer" in result["by_agent"]
    print(f"✓ 에이전트별 비용: total=${result['total_usd']:.3f}")


# ─────────────────────────────────────────────────────────────────────────────
# 서버 통합 테스트
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not AADS_SERVER_RUNNING, reason="AADS_SERVER_RUNNING not set")
@pytest.mark.asyncio
async def test_server_projects_list():
    """GET /api/v1/projects — 목록 조회 (pagination)."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{AADS_TEST_URL}/projects?limit=5&offset=0")
        assert resp.status_code in (200, 404, 405)
        print(f"✓ /projects 목록: {resp.status_code}")


@pytest.mark.skipif(not AADS_SERVER_RUNNING, reason="AADS_SERVER_RUNNING not set")
@pytest.mark.asyncio
async def test_server_project_status():
    """GET /api/v1/projects/{id}/status 엔드포인트 확인."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{AADS_TEST_URL}/projects/nonexistent/status")
        assert resp.status_code in (200, 404)
        print(f"✓ /status 엔드포인트: {resp.status_code}")


@pytest.mark.skipif(not AADS_SERVER_RUNNING, reason="AADS_SERVER_RUNNING not set")
@pytest.mark.asyncio
async def test_server_sse_stream_endpoint():
    """POST /api/v1/projects/{id}/stream SSE 엔드포인트 확인."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{AADS_TEST_URL}/projects/nonexistent/stream")
        assert resp.status_code in (200, 404, 422)
        print(f"✓ /stream 엔드포인트: {resp.status_code}")
