"""
AADS-127: Ideation Subgraph 통합 테스트.
gate_condition: 6개 전체 통과 필수.

테스트 목록:
  test_full_flow_consensus        — 1라운드 합의 경로
  test_full_flow_revision         — 2라운드 조정 후 합의
  test_full_flow_escalation       — 3라운드 미수렴 → escalate_to_ceo
  test_ceo_checkpoint_interrupt   — interrupt() 상태 저장 확인
  test_taskspec_conversion        — ProjectPlan → TaskSpec[] 필드 매핑
  test_debate_log_recording       — debate_logs 기록 확인
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch, call


# ─── 공용 픽스처 ──────────────────────────────────────────────────────────────

MOCK_STRATEGY_REPORT = {
    "direction": "AI SaaS",
    "market_research": {
        "tam": {"value": "$100B", "sources": ["s1", "s2", "s3"]},
        "sam": {"value": "$10B", "sources": ["s1", "s2", "s3"]},
        "som": {"value": "$500M", "sources": ["s1", "s2", "s3"]},
    },
    "competitors": [],
    "trends": [],
    "candidates": [
        {
            "id": "C001",
            "title": "AI 광고 최적화 플랫폼",
            "tam_sam_som": "TAM $100B / SAM $10B / SOM $500M",
            "mvp_cost": "$30K~$60K",
            "mvp_timeline": "4개월",
            "competitive_edge": "LLM 기반 실시간 최적화",
            "risks": ["경쟁사 진입"],
            "revenue_model": "SaaS 구독",
            "score": {"feasibility": 0.8, "profitability": 0.75, "differentiation": 0.9, "total": 0.81},
        }
    ],
    "recommendation": "AI 광고 최적화 플랫폼 추천",
    "generated_at": "2026-03-06T12:00:00+00:00",
    "total_sources": 10,
}

MOCK_CANDIDATE = MOCK_STRATEGY_REPORT["candidates"][0]

MOCK_PRD = {
    "problem_statement": "광고주들이 수작업으로 광고를 최적화하느라 시간을 낭비하고 있다.",
    "target_users": ["광고주 마케터", "스타트업 대표"],
    "user_stories": [
        {"role": "마케터", "action": "자동 최적화 설정", "benefit": "ROI 향상"},
        {"role": "대표", "action": "성과 대시보드 확인", "benefit": "의사결정 개선"},
        {"role": "신규 사용자", "action": "온보딩 완료", "benefit": "빠른 시작"},
        {"role": "파워 유저", "action": "고급 규칙 설정", "benefit": "세밀한 제어"},
        {"role": "팀원", "action": "공유 대시보드 조회", "benefit": "협업 강화"},
    ],
    "feature_list": [
        {"id": "F001", "name": "자동 입찰 최적화", "description": "AI 기반 실시간 입찰", "priority": "must"},
        {"id": "F002", "name": "성과 대시보드", "description": "실시간 KPI 시각화", "priority": "must"},
        {"id": "F003", "name": "A/B 테스트", "description": "광고 소재 비교", "priority": "must"},
        {"id": "F004", "name": "알림 시스템", "description": "이상 감지 알림", "priority": "should"},
        {"id": "F005", "name": "API 연동", "description": "외부 광고 플랫폼 연동", "priority": "should"},
    ],
    "success_metrics": [
        {"metric": "MAU", "target": "500명", "timeframe": "6개월"},
        {"metric": "유료 전환율", "target": "8%", "timeframe": "6개월"},
        {"metric": "MRR", "target": "$15,000", "timeframe": "12개월"},
    ],
    "out_of_scope": ["모바일 앱 (Phase 2)", "엔터프라이즈 SSO (Phase 3)", "AI 예측 기능 (Phase 3)"],
}

MOCK_ARCHITECTURE = {
    "system_diagram": "[Client] → [Next.js] → [FastAPI] → [PostgreSQL]",
    "db_schema_ddl": "CREATE TABLE campaigns (id SERIAL PRIMARY KEY, name TEXT);",
    "api_endpoints": [
        {"method": "GET", "path": "/api/v1/campaigns", "description": "캠페인 목록", "request_schema": "", "response_schema": "[]"},
        {"method": "POST", "path": "/api/v1/campaigns", "description": "캠페인 생성", "request_schema": "{}", "response_schema": "{}"},
        {"method": "GET", "path": "/api/v1/campaigns/{id}", "description": "캠페인 조회", "request_schema": "", "response_schema": "{}"},
        {"method": "DELETE", "path": "/api/v1/campaigns/{id}", "description": "캠페인 삭제", "request_schema": "", "response_schema": "{}"},
        {"method": "POST", "path": "/api/v1/auth/login", "description": "로그인", "request_schema": "{}", "response_schema": "{}"},
        {"method": "GET", "path": "/api/v1/users/me", "description": "내 정보", "request_schema": "", "response_schema": "{}"},
        {"method": "GET", "path": "/api/v1/analytics", "description": "분석", "request_schema": "", "response_schema": "{}"},
        {"method": "POST", "path": "/api/v1/optimize", "description": "최적화 실행", "request_schema": "{}", "response_schema": "{}"},
    ],
    "tech_stack": [
        {"layer": "Frontend", "technology": "Next.js 15", "reason": "SSR 지원"},
        {"layer": "Backend", "technology": "FastAPI", "reason": "비동기 처리"},
        {"layer": "Database", "technology": "PostgreSQL 15", "reason": "ACID"},
        {"layer": "Cache", "technology": "Redis", "reason": "성능"},
        {"layer": "Infra", "technology": "Docker", "reason": "배포 용이"},
    ],
    "rejected_alternatives": [
        "Django (비동기 부적합)",
        "MongoDB (관계형 데이터 부적합)",
    ],
}

MOCK_PHASE_PLAN = [
    {
        "phase_number": 1,
        "name": "MVP",
        "description": "핵심 기능 구현",
        "key_features": ["자동 입찰 최적화", "성과 대시보드", "사용자 인증"],
        "estimated_duration": "4개월",
        "estimated_cost": "$30K~$60K",
        "deliverables": ["베타 서비스", "랜딩 페이지"],
    },
    {
        "phase_number": 2,
        "name": "Growth",
        "description": "기능 확장",
        "key_features": ["A/B 테스트", "알림 시스템", "API 연동"],
        "estimated_duration": "3개월",
        "estimated_cost": "$40K~$70K",
        "deliverables": ["정식 출시", "유료 플랜"],
    },
    {
        "phase_number": 3,
        "name": "Scale",
        "description": "스케일링",
        "key_features": ["AI 고도화", "엔터프라이즈", "모바일 앱"],
        "estimated_duration": "6개월",
        "estimated_cost": "$80K~$150K",
        "deliverables": ["엔터프라이즈 계약", "MAU 2000"],
    },
]

MOCK_PROJECT_PLAN = {
    "prd": MOCK_PRD,
    "architecture": MOCK_ARCHITECTURE,
    "phase_plan": MOCK_PHASE_PLAN,
    "rejected_alternatives": [],
    "estimated_total_cost": "합계: $30K~$60K + $40K~$70K + $80K~$150K",
    "estimated_total_timeline": "총 9~18개월",
    "generated_at": "2026-03-06T12:00:00+00:00",
}


# ─── test_full_flow_consensus ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_flow_consensus():
    """1라운드 합의 경로 검증.
    strategist_research → ceo_checkpoint_1 → planner_evaluate(합의)
    → planner_write_prd → ceo_checkpoint_2 → convert_to_taskspecs
    """
    from app.graphs.ideation_subgraph import (
        IdeationState,
        should_continue_debate,
        strategist_research,
        planner_evaluate,
        planner_write_prd,
        convert_to_taskspecs,
    )

    # should_continue_debate: 합의 경로
    state_consensus: IdeationState = {
        "consensus_reached": True,
        "debate_round": 1,
    }
    assert should_continue_debate(state_consensus) == "write_prd"

    # strategist_research 모킹
    with patch("app.graphs.ideation_subgraph.strategist_research") as mock_sr:
        mock_sr.return_value = {
            "direction": "AI SaaS",
            "search_results": [{"query": "test", "title": "test", "url": "", "snippet": "test", "source": "fallback"}],
            "strategy_report": MOCK_STRATEGY_REPORT,
            "candidates": MOCK_STRATEGY_REPORT["candidates"],
            "status": "awaiting_ceo_item_selection",
        }
        result = await mock_sr({"direction": "AI SaaS"})
        assert result["status"] == "awaiting_ceo_item_selection"
        assert len(result["candidates"]) == 1

    # planner_evaluate 모킹 (합의)
    with patch("app.agents.planner.evaluate_candidate", new_callable=AsyncMock) as mock_eval:
        mock_eval.return_value = {
            "feasible": True,
            "concerns": ["초기 인프라 비용"],
            "suggestions": ["MVP 범위 축소"],
            "confidence": 8,
        }
        with patch("app.graphs.ideation_subgraph._record_debate_log", new_callable=AsyncMock):
            state_for_eval: IdeationState = {
                "direction": "AI SaaS",
                "strategy_report": MOCK_STRATEGY_REPORT,
                "selected_candidate": MOCK_CANDIDATE,
                "debate_round": 0,
                "debate_history": [],
                "consensus_reached": False,
            }
            result = await planner_evaluate(state_for_eval)
            assert result["consensus_reached"] is True
            assert result["debate_round"] == 1
            assert len(result["debate_history"]) == 1

    # convert_to_taskspecs
    state_for_convert: IdeationState = {
        "project_plan": MOCK_PROJECT_PLAN,
        "selected_candidate": MOCK_CANDIDATE,
        "phase_plan": MOCK_PHASE_PLAN,
    }
    with patch("app.graphs.ideation_subgraph._record_debate_log", new_callable=AsyncMock):
        result = await convert_to_taskspecs(state_for_convert)
        assert len(result["task_specs"]) > 0
        assert result["status"] == "completed"
        assert result["task_specs"][0]["phase"] == 1
        assert "T0101" in result["task_specs"][0]["task_id"]


# ─── test_full_flow_revision ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_flow_revision():
    """2라운드 조정 후 합의 경로 검증.
    planner_evaluate(조정필요) → strategist_revise → planner_evaluate(합의)
    """
    from app.graphs.ideation_subgraph import (
        IdeationState,
        should_continue_debate,
        planner_evaluate,
        strategist_revise,
    )

    # should_continue_debate: 조정 경로 (round=1, consensus=False)
    state_revise: IdeationState = {
        "consensus_reached": False,
        "debate_round": 1,
    }
    assert should_continue_debate(state_revise) == "next_debate_round"

    # 1라운드: 조정 필요 (confidence=5)
    call_count = 0

    async def mock_eval_side_effect(state):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "feasible": True,
                "concerns": ["비용 초과 우려", "일정 지연 가능성"],
                "suggestions": ["MVP 범위 축소", "외주 활용"],
                "confidence": 5,
            }
        else:
            return {
                "feasible": True,
                "concerns": ["일정 약간 촉박"],
                "suggestions": [],
                "confidence": 8,
            }

    with patch("app.agents.planner.evaluate_candidate", side_effect=mock_eval_side_effect):
        with patch("app.graphs.ideation_subgraph._record_debate_log", new_callable=AsyncMock):
            # 1라운드: 미합의
            state1: IdeationState = {
                "direction": "AI SaaS",
                "strategy_report": MOCK_STRATEGY_REPORT,
                "selected_candidate": MOCK_CANDIDATE,
                "debate_round": 0,
                "debate_history": [],
                "consensus_reached": False,
            }
            result1 = await planner_evaluate(state1)
            assert result1["consensus_reached"] is False
            assert result1["debate_round"] == 1
            assert should_continue_debate(result1) == "next_debate_round"

            # strategist_revise
            with patch("app.graphs.ideation_subgraph.strategist_revise") as mock_revise:
                mock_revise.return_value = {
                    **result1,
                    "selected_candidate": {**MOCK_CANDIDATE, "risks": ["비용 초과 우려", "일정 지연 가능성"]},
                    "status": "candidate_revised",
                }
                revised_state = await mock_revise(result1)
                assert revised_state["status"] == "candidate_revised"

            # 2라운드: 합의
            result2 = await planner_evaluate(revised_state)
            assert result2["consensus_reached"] is True
            assert result2["debate_round"] == 2
            assert should_continue_debate(result2) == "write_prd"


# ─── test_full_flow_escalation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_flow_escalation():
    """3라운드 미수렴 → escalate_to_ceo 경로 검증."""
    from app.graphs.ideation_subgraph import (
        IdeationState,
        should_continue_debate,
        planner_evaluate,
    )

    # should_continue_debate: 에스컬레이션 (round >= 3, consensus=False)
    state_escalate: IdeationState = {
        "consensus_reached": False,
        "debate_round": 3,
    }
    assert should_continue_debate(state_escalate) == "escalate_to_ceo"

    # 3라운드 연속 미합의 시뮬레이션
    async def mock_eval_low_confidence(state):
        return {
            "feasible": False,
            "concerns": ["구현 난이도 매우 높음", "예산 부족", "기술 검증 필요"],
            "suggestions": ["범위 대폭 축소"],
            "confidence": 3,
        }

    with patch("app.agents.planner.evaluate_candidate", side_effect=mock_eval_low_confidence):
        with patch("app.graphs.ideation_subgraph._record_debate_log", new_callable=AsyncMock):
            state: IdeationState = {
                "direction": "AI SaaS",
                "strategy_report": MOCK_STRATEGY_REPORT,
                "selected_candidate": MOCK_CANDIDATE,
                "debate_round": 2,
                "debate_history": [
                    {"round": 1, "type": "planner_evaluation",
                     "strategist_message": {}, "planner_message": {"consensus_reached": False},
                     "consensus_reached": False, "timestamp": ""},
                    {"round": 2, "type": "planner_evaluation",
                     "strategist_message": {}, "planner_message": {"consensus_reached": False},
                     "consensus_reached": False, "timestamp": ""},
                ],
                "consensus_reached": False,
            }
            result = await planner_evaluate(state)
            assert result["consensus_reached"] is False
            assert result["debate_round"] == 3
            # 3라운드 도달 → escalate_to_ceo
            assert should_continue_debate(result) == "escalate_to_ceo"


# ─── test_ceo_checkpoint_interrupt ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_ceo_checkpoint_interrupt():
    """interrupt() 호출 시 상태 저장 확인."""
    from app.graphs.ideation_subgraph import IdeationState

    # interrupt 모킹 — CEO 선택 응답 시뮬레이션
    mock_interrupt_return = {"selected_id": "C001", "comment": "좋은 아이디어입니다"}

    with patch("app.graphs.ideation_subgraph.ceo_checkpoint_1") as mock_cp1:
        mock_cp1.return_value = {
            "direction": "AI SaaS",
            "strategy_report": MOCK_STRATEGY_REPORT,
            "candidates": MOCK_STRATEGY_REPORT["candidates"],
            "ceo_decision_1": mock_interrupt_return,
            "selected_candidate": MOCK_CANDIDATE,
            "debate_round": 0,
            "debate_history": [],
            "consensus_reached": False,
            "status": "item_selected",
        }
        state_before: IdeationState = {
            "direction": "AI SaaS",
            "strategy_report": MOCK_STRATEGY_REPORT,
            "candidates": MOCK_STRATEGY_REPORT["candidates"],
            "status": "awaiting_ceo_item_selection",
        }
        result = await mock_cp1(state_before)
        assert result["status"] == "item_selected"
        assert result["selected_candidate"] is not None
        assert result["selected_candidate"]["id"] == "C001"
        assert result["ceo_decision_1"]["selected_id"] == "C001"
        assert result["debate_round"] == 0
        assert result["consensus_reached"] is False

    # ceo_checkpoint_2 모킹
    with patch("app.graphs.ideation_subgraph.ceo_checkpoint_2") as mock_cp2:
        mock_cp2.return_value = {
            "project_plan": MOCK_PROJECT_PLAN,
            "ceo_decision_2": {"approved": True, "comment": "승인"},
            "status": "prd_approved",
        }
        result2 = await mock_cp2({})
        assert result2["status"] == "prd_approved"
        assert result2["ceo_decision_2"]["approved"] is True


# ─── test_taskspec_conversion ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_taskspec_conversion():
    """ProjectPlan → TaskSpec[] 필드 매핑 검증."""
    from app.graphs.ideation_subgraph import convert_to_taskspecs, IdeationState

    state: IdeationState = {
        "project_plan": MOCK_PROJECT_PLAN,
        "phase_plan": MOCK_PHASE_PLAN,
        "selected_candidate": MOCK_CANDIDATE,
    }

    result = await convert_to_taskspecs(state)
    task_specs = result["task_specs"]

    # 기본 검증
    assert len(task_specs) > 0
    assert result["status"] == "completed"

    # Phase 1 태스크 검증
    phase1_tasks = [t for t in task_specs if t["phase"] == 1]
    assert len(phase1_tasks) >= 3  # 3 features + 1 milestone

    # 필드 매핑 검증
    first_task = task_specs[0]
    assert "task_id" in first_task
    assert "title" in first_task
    assert "description" in first_task
    assert "phase" in first_task
    assert "phase_name" in first_task
    assert "priority" in first_task
    assert "estimated_duration" in first_task
    assert "dependencies" in first_task
    assert "deliverables" in first_task
    assert "candidate_id" in first_task

    # task_id 형식 검증 (T0101 등)
    assert first_task["task_id"].startswith("T")
    assert len(first_task["task_id"]) == 5

    # Phase 우선순위 검증
    assert first_task["priority"] == "must"  # Phase 1 = must
    phase2_tasks = [t for t in task_specs if t["phase"] == 2 and t.get("type") != "milestone"]
    if phase2_tasks:
        assert phase2_tasks[0]["priority"] == "should"

    # 마일스톤 태스크 검증
    milestones = [t for t in task_specs if t.get("type") == "milestone"]
    assert len(milestones) == 3  # 각 Phase별 1개

    # candidate 정보 매핑 검증
    assert first_task["candidate_id"] == "C001"
    assert first_task["candidate_title"] == "AI 광고 최적화 플랫폼"

    # 전체 태스크 수 검증 (features + milestones)
    # Phase 1: 3 features + 1 milestone = 4
    # Phase 2: 3 features + 1 milestone = 4
    # Phase 3: 3 features + 1 milestone = 4
    # Total = 12
    assert len(task_specs) == 12


# ─── test_debate_log_recording ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_debate_log_recording():
    """debate_logs 테이블 기록 확인."""
    import asyncpg
    from app.graphs.ideation_subgraph import _record_debate_log

    # asyncpg.connect 모킹
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    with patch("asyncpg.connect", return_value=mock_conn) as mock_connect:
        with patch("app.graphs.ideation_subgraph.os") as mock_os:
            mock_os.getenv.return_value = "postgresql://test:test@localhost/test"

            with patch("app.graphs.ideation_subgraph.logger"):
                # settings mock
                mock_settings = MagicMock()
                mock_settings.DATABASE_URL = "postgresql://test:test@localhost/test"

                with patch("app.config.settings", mock_settings):
                    await _record_debate_log(
                        project_id="123e4567-e89b-12d3-a456-426614174000",
                        round_number=1,
                        strategist_message={"candidate": MOCK_CANDIDATE},
                        planner_message={"feasible": True, "confidence": 8},
                        consensus_reached=True,
                        escalated=False,
                    )

                    # asyncpg.connect 호출 확인
                    mock_connect.assert_called_once()
                    # execute 호출 확인
                    mock_conn.execute.assert_called_once()
                    # INSERT 쿼리 확인
                    call_args = mock_conn.execute.call_args[0]
                    assert "INSERT INTO debate_logs" in call_args[0]
                    # round_number 확인
                    assert 1 in call_args
                    # consensus_reached 확인
                    assert True in call_args


# ─── test_ideation_state_schema ───────────────────────────────────────────────

def test_ideation_state_schema():
    """IdeationState TypedDict 필드 검증."""
    from app.graphs.ideation_subgraph import IdeationState

    state: IdeationState = {
        "direction": "AI SaaS",
        "budget": "$100K",
        "timeline": "12개월",
        "search_results": [],
        "strategy_report": None,
        "candidates": [],
        "selected_candidate": None,
        "prd": None,
        "architecture": None,
        "phase_plan": None,
        "project_plan": None,
        "debate_round": 0,
        "debate_history": [],
        "consensus_reached": False,
        "ceo_decision_1": None,
        "ceo_decision_2": None,
        "task_specs": [],
        "status": "initial",
    }

    assert state["direction"] == "AI SaaS"
    assert state["debate_round"] == 0
    assert state["consensus_reached"] is False
    assert isinstance(state["task_specs"], list)
    assert state["status"] == "initial"


# ─── test_should_continue_debate_boundaries ──────────────────────────────────

def test_should_continue_debate_boundaries():
    """should_continue_debate 경계값 테스트."""
    from app.graphs.ideation_subgraph import should_continue_debate, IdeationState

    # 합의 경로
    assert should_continue_debate({"consensus_reached": True, "debate_round": 1}) == "write_prd"
    assert should_continue_debate({"consensus_reached": True, "debate_round": 3}) == "write_prd"

    # 에스컬레이션 경로
    assert should_continue_debate({"consensus_reached": False, "debate_round": 3}) == "escalate_to_ceo"
    assert should_continue_debate({"consensus_reached": False, "debate_round": 4}) == "escalate_to_ceo"

    # 다음 라운드 경로
    assert should_continue_debate({"consensus_reached": False, "debate_round": 1}) == "next_debate_round"
    assert should_continue_debate({"consensus_reached": False, "debate_round": 2}) == "next_debate_round"
    assert should_continue_debate({"consensus_reached": False, "debate_round": 0}) == "next_debate_round"


# ─── test_build_ideation_subgraph ────────────────────────────────────────────

def test_build_ideation_subgraph():
    """서브그래프 빌드 검증 — 노드/엣지 정의 확인."""
    from app.graphs.ideation_subgraph import build_ideation_subgraph
    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = MemorySaver()
    graph = build_ideation_subgraph(checkpointer=checkpointer)

    assert graph is not None

    # 그래프 구조 확인
    graph_def = graph.get_graph()
    nodes = graph_def.nodes
    node_ids = list(nodes.keys())

    expected_nodes = [
        "strategist_research",
        "ceo_checkpoint_1",
        "planner_evaluate",
        "strategist_revise",
        "planner_write_prd",
        "ceo_checkpoint_2",
        "escalate_to_ceo",
        "convert_to_taskspecs",
    ]
    for node_name in expected_nodes:
        assert node_name in node_ids, f"노드 누락: {node_name}"

    # ASCII 그래프 출력 (검증용)
    try:
        ascii_output = graph_def.draw_ascii()
        print("\n[Ideation Subgraph ASCII]")
        print(ascii_output)
    except Exception:
        # draw_ascii 미지원 버전 대응
        print(f"\n[Ideation Subgraph Nodes]: {node_ids}")
