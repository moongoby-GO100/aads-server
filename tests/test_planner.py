"""
AADS-126: Planner Agent 단위 테스트.
gate_condition: 7개 전체 통과 필수.
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch


# ─── test_planner_state_schema ────────────────────────────────────────────────

def test_planner_state_schema():
    """PlannerState TypedDict 필드 검증."""
    from app.agents.planner import PlannerState

    state: PlannerState = {
        "strategy_report": {"direction": "AI SaaS", "candidates": []},
        "selected_candidate": {"id": "C001", "title": "AI 광고 최적화"},
        "prd": None,
        "architecture": None,
        "phase_plan": None,
        "project_plan": None,
        "debate_round": 0,
        "debate_history": [],
        "consensus_reached": False,
        "planner_feedback": None,
    }

    assert state["strategy_report"]["direction"] == "AI SaaS"
    assert state["selected_candidate"]["id"] == "C001"
    assert state["prd"] is None
    assert state["architecture"] is None
    assert state["phase_plan"] is None
    assert state["project_plan"] is None
    assert state["debate_round"] == 0
    assert isinstance(state["debate_history"], list)
    assert state["consensus_reached"] is False
    assert state["planner_feedback"] is None


# ─── test_evaluate_candidate_mock ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_candidate_mock():
    """evaluate_candidate 응답 스키마 검증 (LLM 모킹)."""
    from app.agents.planner import evaluate_candidate, PlannerState

    mock_eval = {
        "feasible": True,
        "concerns": ["기술 복잡도 높음", "초기 인프라 비용 부담"],
        "suggestions": ["MVP 범위 축소", "클라우드 매니지드 서비스 활용"],
        "confidence": 8,
    }

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content=json.dumps(mock_eval))
    )

    state: PlannerState = {
        "strategy_report": {
            "direction": "AI 퍼포먼스 마케팅 SaaS",
            "recommendation": "C001 추천",
            "candidates": [],
        },
        "selected_candidate": {
            "id": "C001",
            "title": "AI 광고 최적화 플랫폼",
            "mvp_cost": "$30K",
            "mvp_timeline": "4개월",
            "risks": ["경쟁 심화"],
        },
        "debate_round": 0,
        "debate_history": [],
        "consensus_reached": False,
    }

    with patch("app.agents.planner.get_llm_for_agent", return_value=(mock_llm, MagicMock())):
        result = await evaluate_candidate(state)

    # 스키마 검증
    assert "feasible" in result
    assert isinstance(result["feasible"], bool)
    assert "concerns" in result
    assert isinstance(result["concerns"], list)
    assert len(result["concerns"]) >= 1
    assert "suggestions" in result
    assert isinstance(result["suggestions"], list)
    assert "confidence" in result
    assert isinstance(result["confidence"], (int, float))
    assert 0 <= result["confidence"] <= 10


# ─── test_write_prd_sections ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_prd_sections():
    """write_prd 결과 — PRD 6섹션 존재 확인."""
    from app.agents.planner import write_prd, PlannerState

    mock_prd = {
        "problem_statement": "AI 마케팅 도구 부재로 ROI 측정이 어렵습니다. 현재 수동 분석에 의존하는 마케터들이 많습니다.",
        "target_users": ["중소기업 마케터 (20~40대)", "스타트업 그로스 팀"],
        "user_stories": [
            {"role": "마케터", "action": "광고 성과 실시간 확인", "benefit": "즉각적 의사결정"},
            {"role": "팀장", "action": "KPI 대시보드 모니터링", "benefit": "목표 달성 추적"},
            {"role": "대행사", "action": "클라이언트 보고서 자동 생성", "benefit": "시간 절약"},
            {"role": "신규 사용자", "action": "온보딩 완료", "benefit": "빠른 시작"},
            {"role": "파워 유저", "action": "AI 최적화 실행", "benefit": "ROAS 향상"},
        ],
        "feature_list": [
            {"id": "F001", "name": "실시간 대시보드", "description": "광고 지표 시각화", "priority": "must"},
            {"id": "F002", "name": "AI 최적화 엔진", "description": "자동 입찰 최적화", "priority": "must"},
            {"id": "F003", "name": "보고서 자동화", "description": "PDF 보고서 생성", "priority": "must"},
            {"id": "F004", "name": "알림 시스템", "description": "성과 알림", "priority": "should"},
            {"id": "F005", "name": "A/B 테스트", "description": "광고 소재 테스트", "priority": "should"},
        ],
        "success_metrics": [
            {"metric": "MAU", "target": "500명", "timeframe": "6개월"},
            {"metric": "유료 전환율", "target": "5%", "timeframe": "6개월"},
            {"metric": "ROAS 개선", "target": "20%", "timeframe": "3개월"},
        ],
        "out_of_scope": ["모바일 앱", "AI 카피라이팅", "오프라인 광고 통합"],
    }

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content=json.dumps(mock_prd))
    )

    state: PlannerState = {
        "strategy_report": {
            "direction": "AI 퍼포먼스 마케팅 SaaS",
            "competitors": [],
        },
        "selected_candidate": {
            "id": "C001",
            "title": "AI 광고 최적화 플랫폼",
            "revenue_model": "구독 SaaS",
        },
        "debate_round": 0,
        "debate_history": [],
        "consensus_reached": False,
    }

    with patch("app.agents.planner.get_llm_for_agent", return_value=(mock_llm, MagicMock())):
        result = await write_prd(state)

    assert "prd" in result
    prd = result["prd"]

    # 6섹션 존재 확인
    assert "problem_statement" in prd, "problem_statement 섹션 누락"
    assert "target_users" in prd, "target_users 섹션 누락"
    assert "user_stories" in prd, "user_stories 섹션 누락"
    assert "feature_list" in prd, "feature_list 섹션 누락"
    assert "success_metrics" in prd, "success_metrics 섹션 누락"
    assert "out_of_scope" in prd, "out_of_scope 섹션 누락"

    # 내용 검증
    assert isinstance(prd["problem_statement"], str)
    assert len(prd["problem_statement"]) > 0
    assert isinstance(prd["target_users"], list)
    assert isinstance(prd["user_stories"], list)
    assert isinstance(prd["feature_list"], list)
    assert isinstance(prd["success_metrics"], list)
    assert isinstance(prd["out_of_scope"], list)


# ─── test_design_architecture_sections ────────────────────────────────────────

@pytest.mark.asyncio
async def test_design_architecture_sections():
    """design_architecture 결과 — 아키텍처 5섹션 존재 확인."""
    from app.agents.planner import design_architecture, PlannerState

    mock_arch = {
        "system_diagram": """[AI 광고 최적화 시스템]
Client → [CDN] → [Next.js Frontend]
Frontend → [FastAPI Backend]
Backend → [PostgreSQL] + [Redis] + [AI Engine]""",
        "db_schema_ddl": """CREATE TABLE users (id UUID PRIMARY KEY, email TEXT UNIQUE NOT NULL, created_at TIMESTAMP DEFAULT NOW());
CREATE TABLE campaigns (id SERIAL PRIMARY KEY, user_id UUID REFERENCES users(id), name TEXT NOT NULL, budget NUMERIC(10,2), created_at TIMESTAMP DEFAULT NOW());
CREATE TABLE ad_metrics (id SERIAL PRIMARY KEY, campaign_id INTEGER REFERENCES campaigns(id), impressions BIGINT, clicks BIGINT, cost NUMERIC(10,4), recorded_at TIMESTAMP DEFAULT NOW());
CREATE TABLE optimizations (id SERIAL PRIMARY KEY, campaign_id INTEGER REFERENCES campaigns(id), action TEXT, result JSONB, applied_at TIMESTAMP DEFAULT NOW());
CREATE TABLE reports (id SERIAL PRIMARY KEY, user_id UUID REFERENCES users(id), data JSONB, created_at TIMESTAMP DEFAULT NOW());
CREATE INDEX idx_campaigns_user ON campaigns(user_id);
CREATE INDEX idx_metrics_campaign ON ad_metrics(campaign_id);""",
        "api_endpoints": [
            {"method": "POST", "path": "/api/v1/auth/login", "description": "로그인", "request_schema": "{}", "response_schema": "{}"},
            {"method": "GET", "path": "/api/v1/campaigns", "description": "캠페인 목록", "request_schema": "", "response_schema": "[]"},
            {"method": "POST", "path": "/api/v1/campaigns", "description": "캠페인 생성", "request_schema": "{}", "response_schema": "{}"},
            {"method": "GET", "path": "/api/v1/campaigns/{id}/metrics", "description": "성과 조회", "request_schema": "", "response_schema": "{}"},
            {"method": "POST", "path": "/api/v1/optimizations", "description": "최적화 실행", "request_schema": "{}", "response_schema": "{}"},
            {"method": "GET", "path": "/api/v1/reports", "description": "보고서 목록", "request_schema": "", "response_schema": "[]"},
            {"method": "POST", "path": "/api/v1/reports", "description": "보고서 생성", "request_schema": "{}", "response_schema": "{}"},
            {"method": "GET", "path": "/api/v1/dashboard", "description": "대시보드", "request_schema": "", "response_schema": "{}"},
        ],
        "tech_stack": [
            {"layer": "Frontend", "technology": "Next.js 15", "reason": "SSR 지원"},
            {"layer": "Backend", "technology": "FastAPI", "reason": "비동기 처리"},
            {"layer": "Database", "technology": "PostgreSQL 15", "reason": "ACID 보장"},
            {"layer": "Cache", "technology": "Redis", "reason": "성능 최적화"},
            {"layer": "Infrastructure", "technology": "AWS ECS", "reason": "확장성"},
        ],
        "rejected_alternatives": [
            "Django (비동기 처리 복잡)",
            "MongoDB (관계형 데이터 부적합)",
        ],
    }

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content=json.dumps(mock_arch))
    )

    state: PlannerState = {
        "selected_candidate": {
            "id": "C001",
            "title": "AI 광고 최적화 플랫폼",
            "revenue_model": "SaaS",
            "mvp_cost": "$30K",
            "mvp_timeline": "4개월",
        },
        "prd": {
            "feature_list": [
                {"id": "F001", "name": "대시보드", "description": "...", "priority": "must"}
            ],
            "target_users": ["마케터"],
        },
        "strategy_report": {},
        "debate_round": 0,
        "debate_history": [],
        "consensus_reached": False,
    }

    with patch("app.agents.planner.get_llm_for_agent", return_value=(mock_llm, MagicMock())):
        result = await design_architecture(state)

    assert "architecture" in result
    arch = result["architecture"]

    # 5섹션 존재 확인
    assert "system_diagram" in arch, "system_diagram 섹션 누락"
    assert "db_schema_ddl" in arch, "db_schema_ddl 섹션 누락"
    assert "api_endpoints" in arch, "api_endpoints 섹션 누락"
    assert "tech_stack" in arch, "tech_stack 섹션 누락"
    assert "rejected_alternatives" in arch, "rejected_alternatives 섹션 누락"

    assert isinstance(arch["system_diagram"], str)
    assert isinstance(arch["db_schema_ddl"], str)
    assert isinstance(arch["api_endpoints"], list)
    assert isinstance(arch["tech_stack"], list)
    assert isinstance(arch["rejected_alternatives"], list)


# ─── test_phase_plan_structure ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_plan_structure():
    """create_phase_plan 결과 — 3개 Phase 각 필수 필드 확인."""
    from app.agents.planner import create_phase_plan, PlannerState

    mock_phases = [
        {
            "phase_number": 1,
            "name": "MVP",
            "description": "핵심 기능으로 시장 검증",
            "key_features": ["실시간 대시보드", "기본 캠페인 관리", "성과 추적"],
            "estimated_duration": "3개월",
            "estimated_cost": "$20K~$40K",
            "deliverables": ["베타 서비스 오픈", "초기 사용자 100명"],
        },
        {
            "phase_number": 2,
            "name": "Growth",
            "description": "기능 확장 및 유료화",
            "key_features": ["AI 최적화 엔진", "자동 보고서", "알림 시스템"],
            "estimated_duration": "3개월",
            "estimated_cost": "$30K~$60K",
            "deliverables": ["유료 플랜 출시", "MAU 500명"],
        },
        {
            "phase_number": 3,
            "name": "Scale",
            "description": "스케일링 및 엔터프라이즈",
            "key_features": ["엔터프라이즈 플랜", "멀티 채널 통합", "AI 고도화", "API 오픈"],
            "estimated_duration": "6개월",
            "estimated_cost": "$80K~$150K",
            "deliverables": ["엔터프라이즈 5건", "MRR $20K"],
        },
    ]

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content=json.dumps(mock_phases))
    )

    state: PlannerState = {
        "selected_candidate": {
            "id": "C001",
            "title": "AI 광고 최적화",
            "mvp_cost": "$30K",
            "mvp_timeline": "4개월",
        },
        "prd": {
            "feature_list": [
                {"id": "F001", "name": "대시보드", "description": "...", "priority": "must"},
                {"id": "F002", "name": "AI 엔진", "description": "...", "priority": "should"},
            ]
        },
        "architecture": {"tech_stack": [{"layer": "Backend", "technology": "FastAPI", "reason": "빠름"}]},
        "strategy_report": {},
        "debate_round": 0,
        "debate_history": [],
        "consensus_reached": False,
    }

    with patch("app.agents.planner.get_llm_for_agent", return_value=(mock_llm, MagicMock())):
        result = await create_phase_plan(state)

    assert "phase_plan" in result
    phases = result["phase_plan"]

    # 3개 Phase 확인
    assert len(phases) == 3, f"3개 Phase 필요, 현재 {len(phases)}개"

    required_fields = ["phase_number", "name", "description", "key_features", "estimated_duration", "estimated_cost", "deliverables"]
    for phase in phases:
        for field in required_fields:
            assert field in phase, f"Phase {phase.get('phase_number', '?')}: {field} 누락"
        assert isinstance(phase["key_features"], list)
        assert len(phase["key_features"]) >= 3, f"Phase {phase['phase_number']}: key_features 최소 3개 필요"
        assert isinstance(phase["deliverables"], list)
        assert len(phase["deliverables"]) >= 1


# ─── test_project_plan_serialization ─────────────────────────────────────────

def test_project_plan_serialization():
    """ProjectPlan Pydantic JSON 직렬화/역직렬화 검증."""
    from app.agents.planner import (
        ProjectPlan, PRDModel, ArchitectureModel, PhaseModel,
        UserStory, Feature, SuccessMetric, TechStackItem, APIEndpoint,
        AlternativeModel,
    )

    prd = PRDModel(
        problem_statement="AI 마케팅 도구 부재로 인한 ROI 측정 어려움. 수동 분석에 의존 중.",
        target_users=["중소기업 마케터", "스타트업 그로스 팀"],
        user_stories=[
            UserStory(role="마케터", action="광고 성과 확인", benefit="즉각 의사결정"),
            UserStory(role="팀장", action="KPI 모니터링", benefit="목표 달성 추적"),
        ],
        feature_list=[
            Feature(id="F001", name="대시보드", description="실시간 지표", priority="must"),
            Feature(id="F002", name="AI 엔진", description="자동 최적화", priority="must"),
            Feature(id="F003", name="보고서", description="PDF 생성", priority="must"),
        ],
        success_metrics=[
            SuccessMetric(metric="MAU", target="500명", timeframe="6개월"),
            SuccessMetric(metric="전환율", target="5%", timeframe="6개월"),
            SuccessMetric(metric="MRR", target="$10K", timeframe="12개월"),
        ],
        out_of_scope=["모바일 앱", "AI 카피라이팅", "오프라인 광고"],
    )

    arch = ArchitectureModel(
        system_diagram="[Client] → [FastAPI] → [PostgreSQL]",
        db_schema_ddl="CREATE TABLE users (id UUID PRIMARY KEY);",
        api_endpoints=[
            APIEndpoint(method="GET", path="/api/v1/health", description="헬스체크"),
        ],
        tech_stack=[
            TechStackItem(layer="Backend", technology="FastAPI", reason="비동기"),
        ],
        rejected_alternatives=["Django (비동기 부적합)"],
    )

    phases = [
        PhaseModel(
            phase_number=1,
            name="MVP",
            description="핵심 기능 검증",
            key_features=["대시보드", "기본 관리", "성과 추적"],
            estimated_duration="3개월",
            estimated_cost="$30K",
            deliverables=["베타 오픈"],
        ),
        PhaseModel(
            phase_number=2,
            name="Growth",
            description="기능 확장",
            key_features=["AI 엔진", "보고서", "알림"],
            estimated_duration="3개월",
            estimated_cost="$50K",
            deliverables=["유료 플랜"],
        ),
        PhaseModel(
            phase_number=3,
            name="Scale",
            description="스케일링",
            key_features=["엔터프라이즈", "API", "모바일", "AI 고도화"],
            estimated_duration="6개월",
            estimated_cost="$100K",
            deliverables=["엔터프라이즈 5건"],
        ),
    ]

    plan = ProjectPlan(
        prd=prd,
        architecture=arch,
        phase_plan=phases,
        rejected_alternatives=[
            AlternativeModel(name="경쟁 아이템 B", reason_rejected="점수 열위"),
        ],
        estimated_total_cost="$180K",
        estimated_total_timeline="12개월",
    )

    # 직렬화
    plan_dict = plan.model_dump()
    assert "prd" in plan_dict
    assert "architecture" in plan_dict
    assert "phase_plan" in plan_dict
    assert "rejected_alternatives" in plan_dict
    assert "estimated_total_cost" in plan_dict
    assert "estimated_total_timeline" in plan_dict
    assert plan_dict["estimated_total_cost"] == "$180K"

    # JSON 직렬화/역직렬화
    json_str = json.dumps(plan_dict, ensure_ascii=False)
    restored = ProjectPlan.model_validate(json.loads(json_str))

    assert restored.estimated_total_cost == plan.estimated_total_cost
    assert restored.estimated_total_timeline == plan.estimated_total_timeline
    assert len(restored.phase_plan) == 3
    assert restored.prd.problem_statement == prd.problem_statement
    assert len(restored.prd.feature_list) == 3
    assert restored.phase_plan[0].name == "MVP"


# ─── test_debate_feedback_format ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_debate_feedback_format():
    """generate_debate_feedback — planner_feedback 문자열 + concerns 리스트 형식 검증."""
    from app.agents.planner import generate_debate_feedback, PlannerState

    mock_feedback = {
        "feedback": "Round 1 기술 검토 완료. PRD 기능 목록은 MVP 범위로 적절합니다. 아키텍처 선택도 타당합니다. 다만 비용 추정의 정밀도를 높이고 Phase 1 기간을 재검토하는 것을 권장합니다.",
        "concerns": ["비용 추정 정밀도 향상 필요", "Phase 1 기간 타당성 재검토", "DB 스키마 확장성 검토"],
        "suggestions": ["MVP 범위 추가 축소", "기술 스택 사전 검증"],
        "consensus_reached": False,
        "confidence": 7,
    }

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content=json.dumps(mock_feedback))
    )

    state: PlannerState = {
        "project_plan": {
            "prd": {"feature_list": [{"id": "F001", "name": "대시보드", "priority": "must"}]},
            "phase_plan": [{"phase_number": 1, "name": "MVP"}],
            "estimated_total_cost": "$180K",
        },
        "debate_round": 0,
        "debate_history": [],
        "consensus_reached": False,
        "strategy_report": {},
        "selected_candidate": {"id": "C001"},
    }

    with patch("app.agents.planner.get_llm_for_agent", return_value=(mock_llm, MagicMock())):
        result = await generate_debate_feedback(state)

    # planner_feedback 문자열 검증
    assert "planner_feedback" in result
    assert isinstance(result["planner_feedback"], str), "planner_feedback는 문자열이어야 함"
    assert len(result["planner_feedback"]) > 0

    # CONCERNS 섹션 포함 확인
    assert "CONCERNS:" in result["planner_feedback"], "CONCERNS: 섹션 누락"

    # debate_history concerns 리스트 형식 확인
    assert "debate_history" in result
    assert len(result["debate_history"]) == 1
    entry = result["debate_history"][0]
    assert "concerns" in entry
    assert isinstance(entry["concerns"], list), "concerns는 리스트여야 함"
    assert len(entry["concerns"]) >= 1

    # debate_round 증가 확인
    assert result["debate_round"] == 1

    # consensus_reached boolean 확인
    assert isinstance(result["consensus_reached"], bool)
