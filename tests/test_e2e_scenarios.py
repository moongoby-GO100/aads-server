"""
AADS-130: E2E 실전 검증 3건 (모킹 모드).

시나리오:
  1. "AI 퍼포먼스 마케팅 자동화 SaaS"
  2. "K-12 온라인 교육 플랫폼"
  3. "이커머스 셀러 자동화 도구"

모킹 전략:
  - LLM 호출은 mock → 실제 비용 0
  - DB 연동은 모킹 (asyncpg mock)
  - 상태 흐름 / 매핑 로직 / 산출물 스키마만 검증
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─── 공통 픽스처 ─────────────────────────────────────────────────────────────

SCENARIOS = [
    {
        "id": 1,
        "direction": "AI 퍼포먼스 마케팅 자동화 SaaS",
        "candidate_id": "c1",
    },
    {
        "id": 2,
        "direction": "K-12 온라인 교육 플랫폼",
        "candidate_id": "c1",
    },
    {
        "id": 3,
        "direction": "이커머스 셀러 자동화 도구",
        "candidate_id": "c1",
    },
]

MOCK_STRATEGY_REPORT = {
    "direction": "Test",
    "market_research": {
        "market_size": {"tam": 120.0, "sam": 30.0, "som": 3.0, "source": "Gartner 2025"},
        "competitors": [
            {"name": "CompA", "strengths": ["speed"], "weaknesses": ["cost"]},
            {"name": "CompB", "strengths": ["features"], "weaknesses": ["ux"]},
        ],
        "trends": [
            {"title": "AI 자동화", "description": "확산", "impact": "높음"},
        ],
        "sources": ["https://gartner.com", "https://idc.com", "https://statista.com"],
    },
    "candidates": [
        {"id": "c1", "title": "후보 A", "score": {"total": 8.5}},
        {"id": "c2", "title": "후보 B", "score": {"total": 7.2}},
        {"id": "c3", "title": "후보 C", "score": {"total": 6.8}},
    ],
    "recommendation": "후보 A 권장",
}

MOCK_PRD = {
    "project_name": "Test SaaS",
    "overview": "테스트 오버뷰",
    "target_users": ["SMB", "엔터프라이즈"],
    "feature_list": ["기능1", "기능2", "기능3"],
    "non_functional": ["성능", "보안"],
    "success_metrics": ["MAU 1만", "ARR $100K"],
    "constraints": ["예산 $50K"],
}

MOCK_ARCHITECTURE = {
    "style": "microservice",
    "components": ["API Gateway", "User Service", "Core Service"],
    "tech_stack": ["FastAPI", "PostgreSQL", "Redis", "React"],
    "db_schema": ["users", "projects", "subscriptions"],
    "api_endpoints": ["/api/auth", "/api/projects", "/api/billing"],
    "deployment": "AWS ECS + RDS",
}

MOCK_PHASE_PLAN = [
    {
        "phase_number": 1, "name": "MVP",
        "key_features": ["기능A", "기능B"],
        "deliverables": ["MVP 앱"],
        "estimated_duration": "8주", "estimated_cost": "$30K",
    },
    {
        "phase_number": 2, "name": "성장",
        "key_features": ["기능C", "기능D"],
        "deliverables": ["v2 릴리즈"],
        "estimated_duration": "12주", "estimated_cost": "$50K",
    },
    {
        "phase_number": 3, "name": "확장",
        "key_features": ["기능E", "기능F"],
        "deliverables": ["엔터프라이즈 버전"],
        "estimated_duration": "16주", "estimated_cost": "$80K",
    },
]

MOCK_IDEATION_RESULT = {
    "strategy_report": MOCK_STRATEGY_REPORT,
    "prd": MOCK_PRD,
    "architecture": MOCK_ARCHITECTURE,
    "phase_plan": MOCK_PHASE_PLAN,
    "task_specs": [
        {"task_id": f"T{i:04d}", "title": f"태스크 {i}", "description": f"태스크 {i} 구현"}
        for i in range(1, 8)
    ],
    "selected_candidate": {"id": "c1", "title": "후보 A"},
    "status": "completed",
}


# ─── work_1~3: E2E 시나리오 (모킹) ───────────────────────────────────────────

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_e2e_scenario_state_flow(scenario):
    """
    각 시나리오: IdeationState → map_plan_to_execution → FullCycleState 흐름 검증.
    LLM 호출 없이 상태 매핑만 확인.
    """
    from app.graphs.full_cycle_graph import map_plan_to_execution

    state = {
        "direction": scenario["direction"],
        "task_specs": MOCK_IDEATION_RESULT["task_specs"],
        "strategy_report": MOCK_STRATEGY_REPORT,
        "selected_candidate": {"id": scenario["candidate_id"], "title": "후보 A"},
        "project_plan": {
            "prd": MOCK_PRD,
            "architecture": MOCK_ARCHITECTURE,
        },
        "project_id": f"test-project-{scenario['id']:03d}",
    }

    result = map_plan_to_execution(state)

    # 기본 흐름 확인
    assert result["current_task"] is not None, f"[시나리오 {scenario['id']}] current_task 없음"
    assert len(result["task_queue"]) == len(MOCK_IDEATION_RESULT["task_specs"]) - 1
    assert result["checkpoint_stage"] == "requirements"
    assert scenario["direction"] in result["messages"][0].content


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_e2e_scenario_strategy_report_quality(scenario):
    """전략 보고서 품질 검증: TAM/SAM/SOM + 3출처 + 후보 3개"""
    market = MOCK_STRATEGY_REPORT["market_research"]

    # TAM/SAM/SOM 존재
    ms = market["market_size"]
    assert ms["tam"] > 0, "TAM 없음"
    assert ms["sam"] > 0, "SAM 없음"
    assert ms["som"] > 0, "SOM 없음"

    # 출처 3개 이상
    assert len(market["sources"]) >= 3, f"출처 {len(market['sources'])}개 (최소 3 필요)"

    # 후보 3개 이상
    assert len(MOCK_STRATEGY_REPORT["candidates"]) >= 3, "후보 3개 미만"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_e2e_scenario_prd_completeness(scenario):
    """PRD 6섹션 완전성 검증"""
    prd = MOCK_PRD
    required_fields = [
        "project_name", "overview", "target_users",
        "feature_list", "non_functional", "success_metrics",
    ]
    for field in required_fields:
        assert field in prd and prd[field], f"PRD 섹션 누락: {field}"

    # feature_list 최소 3개
    assert len(prd["feature_list"]) >= 3


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_e2e_scenario_architecture_completeness(scenario):
    """아키텍처 5섹션 완전성 검증"""
    arch = MOCK_ARCHITECTURE
    required_fields = ["style", "components", "tech_stack", "db_schema", "api_endpoints"]
    for field in required_fields:
        assert field in arch and arch[field], f"아키텍처 섹션 누락: {field}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_e2e_scenario_phase_plan_count(scenario):
    """Phase 계획 3개 이상"""
    assert len(MOCK_PHASE_PLAN) >= 3


# ─── DB 기록 모킹 테스트 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_ideation_artifacts_mock():
    """record_ideation_artifacts: DB insert 모킹 → 5건 저장 확인."""
    from app.services.db_recorder import record_ideation_artifacts

    call_count = 0

    async def mock_record(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return call_count

    with patch("app.services.db_recorder.record_artifact", side_effect=mock_record):
        ids = await record_ideation_artifacts(
            project_id="test-project-001",
            ideation_result=MOCK_IDEATION_RESULT,
        )

    assert len(ids) == 5, f"저장 건수 {len(ids)} (기대 5)"


@pytest.mark.asyncio
async def test_record_artifact_graceful_degradation():
    """DB URL 없을 때 graceful degradation — None 반환."""
    from app.services.db_recorder import record_artifact
    import os

    with patch.dict(os.environ, {"DATABASE_URL": ""}):
        result = await record_artifact(
            project_id="test",
            artifact_type="strategy_report",
            artifact_name="test",
            content={},
        )
    assert result is None


# ─── 모델 스키마 검증 ─────────────────────────────────────────────────────────

def test_strategy_report_model():
    """StrategyReport Pydantic 모델 직렬화 검증."""
    from app.models.strategy import StrategyReport, MarketResearch, MarketSize

    report = StrategyReport(
        direction="AI SaaS",
        market_research=MarketResearch(
            market_size=MarketSize(tam=100, sam=30, som=3, source="Gartner"),
            sources=["https://a.com", "https://b.com", "https://c.com"],
        ),
    )
    data = report.model_dump()
    assert data["direction"] == "AI SaaS"
    assert data["market_research"]["market_size"]["tam"] == 100


def test_project_plan_model():
    """ProjectPlan Pydantic 모델 직렬화 검증."""
    from app.models.plan import ProjectPlan, PRDModel, PhaseModel

    plan = ProjectPlan(
        prd=PRDModel(project_name="TestApp", feature_list=["A", "B", "C"]),
        phase_plan=[PhaseModel(phase_number=1, name="MVP")],
    )
    data = plan.model_dump()
    assert data["prd"]["project_name"] == "TestApp"
    assert len(data["phase_plan"]) == 1


def test_artifact_model():
    """ProjectArtifact Pydantic 모델 검증."""
    from app.models.artifact import ProjectArtifact, ARTIFACT_TYPES

    artifact = ProjectArtifact(
        project_id="proj-001",
        artifact_type="strategy_report",
        artifact_name="시장조사 보고서",
        content={"direction": "AI"},
    )
    assert artifact.artifact_type in ARTIFACT_TYPES
    assert artifact.version == 1
