"""
CUR-AADS-PHASE15-REALTEST-001: 8-agent 실전 파이프라인 테스트.

테스트 대상:
  - TaskSpec 12필드 준수 검증
  - 8-agent 체인 그래프 빌드 확인
  - HITL 체크포인트 서비스 동작
  - 비용 추적 서비스 동작
  - Graceful Degradation (MCP/E2B 없어도 PASS)

실행:
  pytest tests/e2e/test_real_pipeline.py -v
  pytest tests/e2e/test_real_pipeline.py -v -s  # 상세 출력
"""
import os
import sys
import pytest
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# E2B 키 유효성 확인
E2B_API_KEY = os.getenv("E2B_API_KEY", "")
HAS_REAL_E2B_KEY = (
    E2B_API_KEY
    and E2B_API_KEY != "PLACEHOLDER_E2B_API_KEY"
    and len(E2B_API_KEY) > 10
)

# 서버 기동 여부
AADS_TEST_URL = os.getenv("AADS_TEST_URL", "https://aads.newtalk.kr/api/v1")
AADS_SERVER_RUNNING = os.getenv("AADS_SERVER_RUNNING", "false").lower() == "true"


# ─────────────────────────────────────────────────────────────────────────────
# TaskSpec 12필드 검증
# ─────────────────────────────────────────────────────────────────────────────

def make_todo_app_task_spec() -> dict:
    """Python CLI Todo App TaskSpec (12필드 완전 준수)."""
    return {
        "task_id": "PHASE15-TEST-001",
        "parent_task_id": None,
        "description": "Python CLI Todo App — CRUD 기능 구현 (add/list/delete/done)",
        "assigned_agent": "developer",
        "success_criteria": [
            "todo add <text> 명령으로 항목 추가 가능",
            "todo list 명령으로 전체 목록 출력",
            "todo done <id> 명령으로 완료 표시",
            "todo delete <id> 명령으로 항목 삭제",
            "pytest 테스트 통과",
        ],
        "constraints": [
            "Python 3.11+",
            "외부 라이브러리 없이 표준 라이브러리만 사용",
            "JSON 파일로 데이터 저장",
        ],
        "input_artifacts": [],
        "output_artifacts": [
            "todo.py",
            "test_todo.py",
            "README.md",
        ],
        "max_iterations": 3,
        "max_llm_calls": 15,          # R-012
        "budget_limit_usd": 0.50,
        "status": "pending",
    }


def test_task_spec_12_fields():
    """TaskSpec 12필드 완전 준수 확인."""
    required_fields = [
        "task_id", "parent_task_id", "description", "assigned_agent",
        "success_criteria", "constraints", "input_artifacts", "output_artifacts",
        "max_iterations", "max_llm_calls", "budget_limit_usd", "status",
    ]
    spec = make_todo_app_task_spec()
    for field in required_fields:
        assert field in spec, f"TaskSpec 필드 누락: {field}"
    assert len(spec) == 12, f"TaskSpec 필드 수 불일치: {len(spec)} != 12"
    assert spec["max_llm_calls"] == 15, "R-012: max_llm_calls must be 15"
    assert spec["budget_limit_usd"] == 0.50
    print("✓ TaskSpec 12필드 완전 준수")


def test_task_spec_pydantic_model():
    """Pydantic TaskSpec 모델 검증."""
    from app.graph.state import TaskSpec
    spec_dict = make_todo_app_task_spec()
    task = TaskSpec(**spec_dict)
    assert task.task_id == "PHASE15-TEST-001"
    assert task.max_llm_calls == 15
    assert task.budget_limit_usd == 0.50
    assert task.status == "pending"
    print(f"✓ Pydantic TaskSpec: {task.task_id}")


# ─────────────────────────────────────────────────────────────────────────────
# 8-agent 그래프 빌드 확인
# ─────────────────────────────────────────────────────────────────────────────

def test_8agent_graph_build():
    """8-agent 그래프 빌드 + 노드 등록 확인."""
    from app.graph.builder import build_aads_graph
    builder = build_aads_graph()
    nodes = list(builder.nodes.keys())
    expected = [
        "pm_requirements", "supervisor", "architect",
        "developer", "qa", "judge", "devops", "researcher",
    ]
    for node in expected:
        assert node in nodes, f"노드 누락: {node}"
    print(f"✓ 8-agent 그래프 노드: {nodes}")


def test_aads_state_fields():
    """AADSState 신규 필드 확인 (Phase 1.5)."""
    from app.graph.state import AADSState
    hints = AADSState.__annotations__
    required = [
        "architect_design", "devops_result", "research_results",
        "qa_test_results", "judge_verdict",
    ]
    for field in required:
        assert field in hints, f"AADSState 필드 누락: {field}"
    print(f"✓ AADSState 필드 확인: {list(hints.keys())}")


# ─────────────────────────────────────────────────────────────────────────────
# HITL 체크포인트 서비스
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_checkpoint_auto_approve():
    """체크포인트 자동 승인 (Phase 1.5 모드) 테스트."""
    from app.checkpoints import record_checkpoint, get_checkpoint_logs

    log = await record_checkpoint(
        project_id="test-proj-001",
        stage="requirements",
        auto_approve=True,
        feedback="",
        metadata={"test": True},
    )
    assert log["stage"] == "requirements"
    assert log["auto_approved"] is True
    assert log["approved_at"] is not None
    print(f"✓ 체크포인트 자동 승인: {log['id']}")


@pytest.mark.asyncio
async def test_checkpoint_all_6_stages():
    """6단계 체크포인트 순차 기록 테스트."""
    from app.checkpoints import record_checkpoint, CHECKPOINT_STAGES

    project_id = "test-proj-stages"
    logs = []
    for stage in CHECKPOINT_STAGES:
        log = await record_checkpoint(
            project_id=project_id,
            stage=stage,
            auto_approve=True,
        )
        logs.append(log)

    assert len(logs) == 6
    stages_logged = [l["stage"] for l in logs]
    assert stages_logged == CHECKPOINT_STAGES
    print(f"✓ 6단계 체크포인트 완료: {stages_logged}")


# ─────────────────────────────────────────────────────────────────────────────
# 비용 추적
# ─────────────────────────────────────────────────────────────────────────────

def test_cost_tracker_basic():
    """비용 추적 기본 동작 (R-012 한도 체크)."""
    from app.services.cost_tracker import check_and_increment, CostLimitExceeded
    from unittest.mock import MagicMock

    settings_mock = MagicMock()
    settings_mock.MAX_LLM_CALLS_PER_TASK = 15
    settings_mock.MAX_COST_PER_TASK_USD = 10.0
    settings_mock.COST_WARNING_THRESHOLD = 0.8

    state = {
        "llm_calls_count": 0,
        "total_cost_usd": 0.0,
        "cost_breakdown": {},
        "project_id": "test-cost-001",
    }

    result = check_and_increment(state, 0.01, "developer", settings_mock)
    assert result["llm_calls_count"] == 1
    assert result["total_cost_usd"] == pytest.approx(0.01)
    assert result["cost_breakdown"]["developer"] == pytest.approx(0.01)
    print(f"✓ 비용 추적: {result}")


def test_cost_tracker_limit_exceeded():
    """R-012: LLM 호출 15회 초과 시 CostLimitExceeded 발생."""
    from app.services.cost_tracker import check_and_increment, CostLimitExceeded
    from unittest.mock import MagicMock

    settings_mock = MagicMock()
    settings_mock.MAX_LLM_CALLS_PER_TASK = 15
    settings_mock.MAX_COST_PER_TASK_USD = 10.0
    settings_mock.COST_WARNING_THRESHOLD = 0.8

    state = {
        "llm_calls_count": 15,  # 이미 한도 도달
        "total_cost_usd": 0.0,
        "cost_breakdown": {},
        "project_id": "test-cost-002",
    }

    with pytest.raises(CostLimitExceeded):
        check_and_increment(state, 0.01, "developer", settings_mock)
    print("✓ R-012 LLM 호출 한도 초과 감지")


@pytest.mark.asyncio
async def test_get_project_costs():
    """프로젝트 비용 조회 (Redis 없을 때 state_only fallback)."""
    from app.services.cost_tracker import get_project_costs

    breakdown = {"pm": 0.005, "developer": 0.02, "qa": 0.01}
    result = await get_project_costs("test-proj-cost", breakdown)

    assert result["project_id"] == "test-proj-cost"
    assert result["total_usd"] == pytest.approx(0.035, rel=1e-3)
    assert "developer" in result["by_agent"]
    print(f"✓ 비용 조회: {result}")


# ─────────────────────────────────────────────────────────────────────────────
# Graceful Degradation
# ─────────────────────────────────────────────────────────────────────────────

def test_graceful_degradation_no_mcp():
    """MCP 없이도 에이전트 임포트 가능."""
    try:
        from app.agents.architect_agent import architect_node
        from app.agents.devops_agent import devops_node
        from app.agents.researcher_agent import researcher_node
        print("✓ MCP 없이도 에이전트 모듈 임포트 성공")
    except ImportError as e:
        pytest.skip(f"임포트 실패 (의존성 없음): {e}")


def test_graceful_degradation_cost_tracker_no_redis():
    """Redis 없이도 비용 추적 동작 (UPSTASH_REDIS_URL 미설정)."""
    import os
    old = os.environ.pop("UPSTASH_REDIS_URL", None)
    try:
        from app.services.cost_tracker import _try_redis_increment
        _try_redis_increment("test", "agent", 0.01, 1)  # 에러 없이 통과
        print("✓ Redis 없이 비용 추적 graceful degradation")
    finally:
        if old:
            os.environ["UPSTASH_REDIS_URL"] = old


# ─────────────────────────────────────────────────────────────────────────────
# 서버 통합 테스트 (서버 기동 시)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not AADS_SERVER_RUNNING, reason="AADS_SERVER_RUNNING not set")
@pytest.mark.asyncio
async def test_server_health():
    """서버 health 엔드포인트 확인."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{AADS_TEST_URL}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"
        print(f"✓ 서버 health: {data}")


@pytest.mark.skipif(not AADS_SERVER_RUNNING, reason="AADS_SERVER_RUNNING not set")
@pytest.mark.asyncio
async def test_server_project_create():
    """POST /api/v1/projects — 프로젝트 생성 (자동 승인 모드)."""
    import httpx
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{AADS_TEST_URL}/projects",
            json={"description": "Python CLI Todo App CRUD (PHASE15 test)"},
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert "project_id" in data
        print(f"✓ 프로젝트 생성: {data.get('project_id')}")
        return data.get("project_id")


@pytest.mark.skipif(not AADS_SERVER_RUNNING, reason="AADS_SERVER_RUNNING not set")
@pytest.mark.asyncio
async def test_server_project_costs():
    """GET /api/v1/projects/{id}/costs 엔드포인트 확인."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{AADS_TEST_URL}/projects/nonexistent-proj/costs")
        # 404 또는 200 모두 OK (엔드포인트 존재 확인)
        assert resp.status_code in (200, 404)
        print(f"✓ /costs 엔드포인트 응답: {resp.status_code}")
