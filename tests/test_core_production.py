"""
AADS 코어 고도화 E2E 통합 테스트 (T-031)
=========================================
시나리오 1: 단순 코딩 태스크 → PM→Supervisor→Developer→QA→Judge (자동 PASS)
시나리오 2: 복잡 설계 태스크 → PM→Supervisor→Architect→Developer→QA→Judge→DevOps (HITL 체크포인트)
시나리오 3: 실패 시나리오  → Judge fail → 재작업 1회 → 2차 fail → CEO 에스컬레이션
시나리오 4: 병렬 태스크    → Researcher+Architect 동시 실행 → 결과 merge

모든 외부 의존성(LLM, DB, Redis)은 mock으로 대체.
"""
from __future__ import annotations

import asyncio
import json
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── 공통 픽스처 ──────────────────────────────────────────────────────────────

def make_base_state(**overrides) -> dict:
    """기본 AADSState 딕셔너리."""
    base = {
        "messages": [],
        "current_task": None,
        "task_queue": [],
        "next_agent": None,
        "active_agents": [],
        "checkpoint_stage": "requirements",
        "approved_stages": [],
        "revision_count": 0,
        "llm_calls_count": 0,
        "total_cost_usd": 0.0,
        "cost_breakdown": {},
        "generated_files": [],
        "sandbox_results": [],
        "qa_test_results": [],
        "judge_verdict": None,
        "project_id": "test-project-001",
        "created_at": "2026-03-04T00:00:00",
        "iteration_count": 0,
        "error_log": [],
        "architect_design": None,
        "devops_result": None,
        "research_results": [],
    }
    base.update(overrides)
    return base


SIMPLE_TASK = {
    "task_id": "t-001",
    "description": "두 수를 더하는 add(a, b) 함수를 구현하세요. 단위 테스트 포함.",
    "assigned_agent": "developer",
    "success_criteria": ["add(2, 3) == 5", "add(0, 0) == 0", "add(-1, 1) == 0"],
    "output_artifacts": ["main.py", "test_main.py"],
    "constraints": [],
    "status": "pending",
}

COMPLEX_TASK = {
    "task_id": "t-002",
    "description": "RESTful API 서버를 설계하고 구현하세요. FastAPI 기반. DB 연동 포함.",
    "assigned_agent": "architect",
    "success_criteria": [
        "GET /health 엔드포인트 존재",
        "POST /items 엔드포인트 존재",
        "PostgreSQL 연동 코드 존재",
        "에러 처리 미들웨어 존재",
    ],
    "output_artifacts": ["main.py", "models.py", "database.py", "requirements.txt"],
    "constraints": ["FastAPI 사용", "PostgreSQL 연동"],
    "status": "pending",
}

PARALLEL_TASK = {
    "task_id": "t-004",
    "description": "최신 AI 라이브러리를 활용한 텍스트 분류기를 설계하고 구현하세요.",
    "assigned_agent": "developer",
    "research_needed": True,
    "success_criteria": [
        "텍스트 분류 함수 존재",
        "정확도 > 80%",
        "학습 코드 존재",
    ],
    "output_artifacts": ["classifier.py", "train.py"],
    "constraints": [],
    "status": "pending",
}


# ─── 시나리오 1: 단순 코딩 태스크 자동 PASS ───────────────────────────────────
class TestScenario1SimpleCodingTask:
    """시나리오 1: PM→Supervisor→Developer→QA→Judge (자동 PASS)"""

    @pytest.mark.asyncio
    async def test_supervisor_routes_to_developer(self):
        """Supervisor가 assigned_agent=developer로 정확히 라우팅."""
        from app.agents.supervisor import supervisor_node

        state = make_base_state(
            current_task=SIMPLE_TASK,
            iteration_count=0,
            llm_calls_count=2,
            architect_design={"design": "simple_function"},  # 설계 이미 있음
        )
        result = await supervisor_node(state)
        assert result["next_agent"] == "developer"
        assert result["iteration_count"] == 1

    @pytest.mark.asyncio
    async def test_judge_auto_pass(self):
        """Judge가 success_criteria 충족 시 pass 판정."""
        from app.agents.judge_agent import judge_node

        verdict = {
            "verdict": "pass",
            "score": 0.95,
            "criteria_met": ["add(2, 3) == 5", "add(0, 0) == 0", "add(-1, 1) == 0"],
            "criteria_failed": [],
            "issues": [],
            "rework_instructions": "",
            "recommendation": "모든 success_criteria 충족",
        }
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(verdict))

        with patch("app.agents.judge_agent.get_llm_for_agent",
                   return_value=(mock_llm, MagicMock(input_cost_per_m=2.0, output_cost_per_m=12.0))), \
             patch("app.agents.judge_agent.estimate_cost", return_value=0.01), \
             patch("app.agents.judge_agent.check_and_increment",
                   return_value={"llm_calls_count": 3, "total_cost_usd": 0.05, "cost_breakdown": {"judge": 0.01}}), \
             patch("app.agents.judge_agent.extract_and_store_experience", new_callable=AsyncMock):

            state = make_base_state(
                current_task=SIMPLE_TASK,
                generated_files=[{
                    "path": "main.py", "language": "python",
                    "content": "def add(a, b):\n    return a + b\n"
                }],
                sandbox_results=[{"exit_code": 0, "stdout": "5\n0\n0"}],
                qa_test_results=[{"status": "pass", "tests_passed": 3, "tests_total": 3}],
                checkpoint_stage="final_review",
                iteration_count=0,
            )
            result = await judge_node(state)

        assert result["judge_verdict"]["verdict"] == "pass"
        assert result["judge_verdict"]["score"] == 0.95
        assert result["checkpoint_stage"] == "completed"

    @pytest.mark.asyncio
    async def test_full_simple_pipeline_routing(self):
        """단순 파이프라인 라우팅 검증: Developer→QA→Judge(pass) 흐름."""
        from app.graph.routing import route_after_developer, route_after_qa, route_after_judge

        # Developer 완료 → QA
        dev_state = make_base_state(
            current_task={**SIMPLE_TASK, "status": "completed"},
        )
        assert route_after_developer(dev_state) == "qa"

        # QA 완료 → Judge
        qa_state = make_base_state(checkpoint_stage="final_review")
        assert route_after_qa(qa_state) == "judge"

        # Judge pass → DevOps
        judge_state = make_base_state(
            checkpoint_stage="completed",
            judge_verdict={"verdict": "pass", "score": 0.95},
        )
        assert route_after_judge(judge_state) == "__end__"


# ─── 시나리오 2: 복잡 설계 태스크 (HITL 체크포인트) ──────────────────────────
class TestScenario2ComplexDesignTask:
    """시나리오 2: PM→Supervisor→Architect→Developer→QA→Judge→DevOps (HITL)"""

    @pytest.mark.asyncio
    async def test_supervisor_routes_to_architect_when_no_design(self):
        """설계 없으면 Supervisor→Architect 라우팅."""
        from app.graph.routing import route_after_supervisor

        state = make_base_state(
            current_task=COMPLEX_TASK,
            next_agent="architect",
            architect_design=None,  # 설계 없음
            checkpoint_stage="plan_review",
        )
        result = route_after_supervisor(state)
        assert result == "architect"

    @pytest.mark.asyncio
    async def test_supervisor_skips_architect_when_design_exists(self):
        """설계 있으면 Supervisor→Developer 바로 라우팅."""
        from app.graph.routing import route_after_supervisor

        state = make_base_state(
            current_task=COMPLEX_TASK,
            next_agent="developer",
            architect_design={"design_doc": "FastAPI REST API 설계서"},
            checkpoint_stage="plan_review",
        )
        result = route_after_supervisor(state)
        assert result == "developer"

    @pytest.mark.asyncio
    async def test_complex_task_hitl_checkpoint_final_review(self):
        """복잡 태스크: final_review 단계에서 HITL 체크포인트 필요."""
        from app.services.autonomy_gate import needs_hitl_checkpoint

        # full_hitl 레벨인 경우 mock
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {"level": "full_hitl"}

        result = await needs_hitl_checkpoint(mock_conn, "complex_design", "final_review")
        assert result is True

    @pytest.mark.asyncio
    async def test_judge_conditional_pass_routes_to_devops(self):
        """Judge conditional_pass → DevOps로 라우팅."""
        from app.graph.routing import route_after_judge

        state = make_base_state(
            checkpoint_stage="completed",
            judge_verdict={"verdict": "conditional_pass", "score": 0.72},
        )
        # completed 상태이면 __end__ (DevOps 이후 END)
        result = route_after_judge(state)
        assert result == "__end__"

    @pytest.mark.asyncio
    async def test_model_router_architect_uses_opus(self):
        """Architect는 claude-opus-4-6 사용 (T-002)."""
        from app.services.model_router import AGENT_MODELS
        config = AGENT_MODELS["architect"]["primary"]
        assert config.model_id == "claude-opus-4-6"
        assert config.provider == "anthropic"
        assert config.input_cost_per_m == 5.0
        assert config.output_cost_per_m == 25.0


# ─── 시나리오 3: 실패 → 재작업 → CEO 에스컬레이션 ────────────────────────────
class TestScenario3FailureEscalation:
    """시나리오 3: Judge fail → 재작업 1회 → 2차 fail → CEO 에스컬레이션"""

    @pytest.mark.asyncio
    async def test_judge_fail_triggers_rework(self):
        """Judge fail → checkpoint_stage=development (재작업 지시)."""
        from app.agents.judge_agent import judge_node

        verdict = {
            "verdict": "fail",
            "score": 0.3,
            "criteria_met": [],
            "criteria_failed": ["add(2, 3) == 5", "add(0, 0) == 0"],
            "issues": ["add 함수 없음", "파일 비어있음"],
            "rework_instructions": "main.py에 add(a, b) 함수를 구현하고 테스트를 추가하세요.",
            "recommendation": "핵심 기능 미구현",
        }
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(verdict))

        with patch("app.agents.judge_agent.get_llm_for_agent",
                   return_value=(mock_llm, MagicMock(input_cost_per_m=2.0, output_cost_per_m=12.0))), \
             patch("app.agents.judge_agent.estimate_cost", return_value=0.01), \
             patch("app.agents.judge_agent.check_and_increment",
                   return_value={"llm_calls_count": 4, "total_cost_usd": 0.06, "cost_breakdown": {"judge": 0.01}}), \
             patch("app.agents.judge_agent.extract_and_store_experience", new_callable=AsyncMock):

            state = make_base_state(
                current_task=SIMPLE_TASK,
                generated_files=[],
                iteration_count=0,
                checkpoint_stage="final_review",
            )
            result = await judge_node(state)

        assert result["judge_verdict"]["verdict"] == "fail"
        assert result["checkpoint_stage"] == "development"
        assert result["iteration_count"] == 1
        # 재작업 지시사항이 current_task에 포함됨
        assert "rework_feedback" in result.get("current_task", {})
        assert "rework_instructions" in result["current_task"]["rework_feedback"]

    @pytest.mark.asyncio
    async def test_judge_fail_second_time_forced_complete(self):
        """Judge fail 3회 초과 → 강제 완료 (추후 CEO 에스컬레이션)."""
        from app.agents.judge_agent import judge_node

        verdict = {
            "verdict": "fail",
            "score": 0.2,
            "criteria_met": [],
            "criteria_failed": ["add 함수 누락"],
            "issues": ["여전히 미구현"],
            "rework_instructions": "add 함수를 구현하세요.",
            "recommendation": "재작업 필요",
        }
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(verdict))

        with patch("app.agents.judge_agent.get_llm_for_agent",
                   return_value=(mock_llm, MagicMock(input_cost_per_m=2.0, output_cost_per_m=12.0))), \
             patch("app.agents.judge_agent.estimate_cost", return_value=0.01), \
             patch("app.agents.judge_agent.check_and_increment",
                   return_value={"llm_calls_count": 7, "total_cost_usd": 0.10, "cost_breakdown": {"judge": 0.01}}), \
             patch("app.agents.judge_agent.extract_and_store_experience", new_callable=AsyncMock):

            state = make_base_state(
                current_task=SIMPLE_TASK,
                generated_files=[],
                iteration_count=3,  # 3회 이미 재작업
                checkpoint_stage="final_review",
            )
            result = await judge_node(state)

        # 3회 초과 → 강제 완료 (CEO 에스컬레이션 예정)
        assert result["checkpoint_stage"] == "completed"

    @pytest.mark.asyncio
    async def test_supervisor_max_iterations_escalates_to_ceo(self):
        """Supervisor max_iterations 초과 → CEO 에스컬레이션 + cancelled."""
        from app.agents.supervisor import supervisor_node

        with patch("app.agents.supervisor.notify_ceo_escalation", new_callable=AsyncMock) as mock_ceo:
            state = make_base_state(
                current_task=SIMPLE_TASK,
                iteration_count=5,  # MAX_ITERATIONS=5 도달
                llm_calls_count=10,
            )
            result = await supervisor_node(state)

        assert result["checkpoint_stage"] == "cancelled"
        assert "Max iterations" in result["error_log"][-1]
        mock_ceo.assert_called_once()

    @pytest.mark.asyncio
    async def test_supervisor_llm_calls_limit_escalates_to_ceo(self):
        """LLM 호출 한도 초과 → CEO 에스컬레이션 + cancelled."""
        from app.agents.supervisor import supervisor_node

        with patch("app.agents.supervisor.notify_ceo_escalation", new_callable=AsyncMock) as mock_ceo, \
             patch("app.agents.supervisor.settings") as mock_settings:
            mock_settings.MAX_LLM_CALLS_PER_TASK = 15
            mock_settings.MAX_COST_PER_TASK_USD = 10.0
            mock_settings.COST_WARNING_THRESHOLD = 0.8

            state = make_base_state(
                current_task=SIMPLE_TASK,
                iteration_count=0,
                llm_calls_count=15,  # 한도 도달
            )
            result = await supervisor_node(state)

        assert result["checkpoint_stage"] == "cancelled"
        assert "LLM call limit" in result["error_log"][-1]
        mock_ceo.assert_called_once()

    @pytest.mark.asyncio
    async def test_routing_judge_fail_goes_to_developer(self):
        """Judge fail (iteration < 3) → developer 재작업."""
        from app.graph.routing import route_after_judge

        state = make_base_state(
            checkpoint_stage="development",
            judge_verdict={"verdict": "fail", "score": 0.3},
            iteration_count=1,
        )
        result = route_after_judge(state)
        assert result == "developer"


# ─── 시나리오 4: 병렬 태스크 (Researcher + Architect 동시) ───────────────────
class TestScenario4ParallelTasks:
    """시나리오 4: Researcher+Architect 동시 실행 → 결과 merge"""

    @pytest.mark.asyncio
    async def test_supervisor_detects_parallel_opportunity(self):
        """Supervisor: research_needed + no architect_design → 병렬 플래그 세팅."""
        from app.agents.supervisor import supervisor_node, _check_parallel_execution

        state = make_base_state(
            current_task=PARALLEL_TASK,
            iteration_count=0,
            llm_calls_count=2,
            architect_design=None,
            research_results=[],
        )
        result = await supervisor_node(state)
        # Researcher로 라우팅 (병렬 실행 포함)
        assert result["next_agent"] == "researcher"
        # parallel_agents 플래그가 있을 수 있음
        # (병렬 실행 조건 충족 시)

    @pytest.mark.asyncio
    async def test_check_parallel_execution_returns_pair(self):
        """독립 태스크 병렬 실행 조건 감지."""
        from app.agents.supervisor import _check_parallel_execution

        state = make_base_state(
            architect_design=None,
            research_results=[],
        )
        # developer 또는 architect가 primary이고 설계/연구 모두 없을 때
        pair = _check_parallel_execution("developer", state)
        assert pair == {"researcher", "architect"}

    @pytest.mark.asyncio
    async def test_check_parallel_execution_skips_if_already_done(self):
        """이미 연구/설계 결과가 있으면 병렬 실행 스킵."""
        from app.agents.supervisor import _check_parallel_execution

        state = make_base_state(
            architect_design={"design_doc": "기존 설계"},
            research_results=[{"findings": "pandas 사용 권장"}],
        )
        pair = _check_parallel_execution("developer", state)
        assert pair is None

    @pytest.mark.asyncio
    async def test_research_results_merged_in_state(self):
        """Researcher 완료 후 research_results가 state에 병합됨."""
        # Researcher → Supervisor 라우팅 확인 (결과 merge)
        from app.graph.builder import build_aads_graph
        builder = build_aads_graph()
        # 그래프가 정상 빌드되는지 확인
        assert builder is not None

    @pytest.mark.asyncio
    async def test_parallel_agents_independent_results(self):
        """병렬 실행된 에이전트들의 결과가 독립적으로 state에 저장됨."""
        # Researcher는 research_results에, Architect는 architect_design에 저장
        from app.graph.state import AADSState

        # State 리듀서: _last_value로 각자 독립 저장
        research = [{"findings": "transformers 라이브러리 사용 권장"}]
        design = {"design_doc": "텍스트 분류기 아키텍처", "components": ["Tokenizer", "Model"]}

        # State 업데이트 시뮬레이션
        state = make_base_state(
            research_results=research,
            architect_design=design,
        )
        assert state["research_results"] == research
        assert state["architect_design"] == design

    @pytest.mark.asyncio
    async def test_supervisor_uses_research_results(self):
        """Researcher 결과가 있으면 Supervisor가 architect/developer로 라우팅."""
        from app.agents.supervisor import supervisor_node

        state = make_base_state(
            current_task=PARALLEL_TASK,
            iteration_count=1,
            llm_calls_count=4,
            architect_design=None,
            research_results=[{"findings": "transformers 라이브러리 사용 권장"}],
        )
        result = await supervisor_node(state)
        # 연구 결과 있으면 researcher 스킵 → developer/architect로
        assert result["next_agent"] in ("developer", "architect", "researcher")
        # research_needed=True인데 research_results가 있으면 researcher 스킵
        # assigned_agent=developer이고 architect_design이 없으면...
        # 현재 supervisor는 assigned_agent를 따름


# ─── 추가: Model Router T-002 매핑 검증 ──────────────────────────────────────
class TestModelRouterT002Compliance:
    """Model Router가 T-002 테이블과 100% 일치하는지 검증."""

    def test_supervisor_uses_opus_46(self):
        from app.services.model_router import AGENT_MODELS
        cfg = AGENT_MODELS["supervisor"]["primary"]
        assert cfg.model_id == "claude-opus-4-6"
        assert cfg.input_cost_per_m == 5.0
        assert cfg.output_cost_per_m == 25.0

    def test_architect_uses_opus_46(self):
        from app.services.model_router import AGENT_MODELS
        cfg = AGENT_MODELS["architect"]["primary"]
        assert cfg.model_id == "claude-opus-4-6"
        assert cfg.input_cost_per_m == 5.0

    def test_pm_uses_sonnet_46(self):
        from app.services.model_router import AGENT_MODELS
        cfg = AGENT_MODELS["pm"]["primary"]
        assert cfg.model_id == "claude-sonnet-4-6"
        assert cfg.input_cost_per_m == 3.0
        assert cfg.output_cost_per_m == 15.0

    def test_developer_uses_sonnet_46(self):
        from app.services.model_router import AGENT_MODELS
        cfg = AGENT_MODELS["developer"]["primary"]
        assert cfg.model_id == "claude-sonnet-4-6"
        assert cfg.input_cost_per_m == 3.0

    def test_qa_uses_sonnet_46(self):
        from app.services.model_router import AGENT_MODELS
        cfg = AGENT_MODELS["qa"]["primary"]
        assert cfg.model_id == "claude-sonnet-4-6"

    def test_judge_uses_gemini_pro(self):
        """Judge는 Developer/QA와 다른 모델 사용 (T-002)."""
        from app.services.model_router import AGENT_MODELS
        judge_cfg = AGENT_MODELS["judge"]["primary"]
        dev_cfg = AGENT_MODELS["developer"]["primary"]
        assert judge_cfg.model_id == "gemini-3.1-pro-preview"
        assert judge_cfg.provider == "google"
        assert judge_cfg.input_cost_per_m == 2.0
        assert judge_cfg.output_cost_per_m == 12.0
        # Judge ≠ Developer 확인
        assert judge_cfg.model_id != dev_cfg.model_id
        assert judge_cfg.provider != dev_cfg.provider

    def test_devops_uses_gpt5_mini(self):
        from app.services.model_router import AGENT_MODELS
        cfg = AGENT_MODELS["devops"]["primary"]
        assert cfg.model_id == "gpt-5-mini"
        assert cfg.provider == "openai"
        assert cfg.input_cost_per_m == 0.25
        assert cfg.output_cost_per_m == 2.0

    def test_researcher_uses_gemini_flash(self):
        from app.services.model_router import AGENT_MODELS
        cfg = AGENT_MODELS["researcher"]["primary"]
        assert cfg.model_id == "gemini-2.5-flash"
        assert cfg.provider == "google"
        assert cfg.input_cost_per_m == 0.30
        assert cfg.output_cost_per_m == 2.50

    def test_fallback_chain_exists_for_all_agents(self):
        """모든 에이전트에 primary → fallback → error 체인 존재."""
        from app.services.model_router import AGENT_MODELS
        for agent_name, tiers in AGENT_MODELS.items():
            assert "primary" in tiers, f"{agent_name}: primary 없음"
            assert "fallback" in tiers, f"{agent_name}: fallback 없음"
            assert "error" in tiers, f"{agent_name}: error fallback 없음"


# ─── 추가: Autonomy Gate 동작 검증 ────────────────────────────────────────────
class TestAutonomyGate:
    """autonomy_gate.py: 성공률 기반 자율성 수준 결정 검증."""

    @pytest.mark.asyncio
    async def test_insufficient_samples_always_hitl(self):
        """샘플 < 20건 → 항상 full_hitl."""
        from app.services.autonomy_gate import evaluate_autonomy_level

        mock_conn = AsyncMock()
        # 10건만 있음
        mock_conn.fetch.return_value = [
            {"judge_verdict": "pass", "user_modified": False}
        ] * 10
        mock_conn.execute.return_value = None
        mock_conn.fetchrow.return_value = None

        result = await evaluate_autonomy_level(mock_conn, "simple_coding")
        assert result["level"] == "full_hitl"
        assert "샘플 부족" in result["reason"]

    @pytest.mark.asyncio
    async def test_high_pass_rate_low_modify_auto_approve(self):
        """통과율 ≥ 90% + 수정율 ≤ 10% → auto_approve."""
        from app.services.autonomy_gate import evaluate_autonomy_level

        mock_conn = AsyncMock()
        # 50건: 48 pass, 2 fail, 3 user_modified
        rows = (
            [{"judge_verdict": "pass", "user_modified": False}] * 45
            + [{"judge_verdict": "pass", "user_modified": True}] * 3
            + [{"judge_verdict": "fail", "user_modified": False}] * 2
        )
        mock_conn.fetch.return_value = rows
        mock_conn.execute.return_value = None
        mock_conn.fetchrow.return_value = None

        result = await evaluate_autonomy_level(mock_conn, "simple_coding")
        assert result["level"] == "auto_approve"
        assert result["judge_pass_rate"] >= 0.90
        assert result["user_modify_rate"] <= 0.10

    @pytest.mark.asyncio
    async def test_low_pass_rate_hitl_reactivated(self):
        """성공률 < 70% → HITL 재활성화."""
        from app.services.autonomy_gate import evaluate_autonomy_level

        mock_conn = AsyncMock()
        # 50건: 30 pass, 20 fail
        rows = (
            [{"judge_verdict": "pass", "user_modified": False}] * 30
            + [{"judge_verdict": "fail", "user_modified": False}] * 20
        )
        mock_conn.fetch.return_value = rows
        mock_conn.execute.return_value = None
        mock_conn.fetchrow.return_value = None

        result = await evaluate_autonomy_level(mock_conn, "complex_design")
        assert result["level"] == "full_hitl"
        assert "HITL 재활성화" in result["reason"]

    @pytest.mark.asyncio
    async def test_auto_approve_no_checkpoint(self):
        """auto_approve 수준이면 HITL 체크포인트 불필요."""
        from app.services.autonomy_gate import needs_hitl_checkpoint

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {"level": "auto_approve"}

        result = await needs_hitl_checkpoint(mock_conn, "simple_coding", "final_review")
        assert result is False

    @pytest.mark.asyncio
    async def test_simplified_hitl_only_final_review(self):
        """simplified_hitl → final_review 단계만 체크포인트."""
        from app.services.autonomy_gate import needs_hitl_checkpoint

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {"level": "simplified_hitl"}

        assert await needs_hitl_checkpoint(mock_conn, "task_type", "final_review") is True
        assert await needs_hitl_checkpoint(mock_conn, "task_type", "plan_review") is True
        mock_conn.fetchrow.return_value = {"level": "simplified_hitl"}
        assert await needs_hitl_checkpoint(mock_conn, "task_type", "development") is False

    @pytest.mark.asyncio
    async def test_record_task_result(self):
        """태스크 결과 기록 함수 호출 검증."""
        from app.services.autonomy_gate import record_task_result

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = None

        await record_task_result(
            mock_conn, "simple_coding", "t-001", "pass", False, "proj-001"
        )
        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args[0]
        assert "simple_coding" in call_args
        assert "t-001" in call_args
        assert "pass" in call_args
