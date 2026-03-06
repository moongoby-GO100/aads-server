"""
AADS-127: Ideation Subgraph — Strategist↔Planner 양방향 토론 루프.

7개 노드 + conditional_edges:
  strategist_research → ceo_checkpoint_1 → planner_evaluate
  → (합의: planner_write_prd → ceo_checkpoint_2 → convert_to_taskspecs)
  → (조정: strategist_revise → planner_evaluate 루프백)
  → (에스컬레이션: escalate_to_ceo → planner_evaluate 재개)

CEO 체크포인트 2곳: interrupt() HITL.
"""
from __future__ import annotations

import json
import os
import structlog
from datetime import datetime, timezone
from typing import Optional
from typing_extensions import TypedDict

logger = structlog.get_logger()

# ─── IdeationState ────────────────────────────────────────────────────────────


class IdeationState(TypedDict, total=False):
    # Strategist 입력
    direction: str
    budget: Optional[str]
    timeline: Optional[str]
    # Strategist 산출물
    search_results: list[dict]
    strategy_report: Optional[dict]
    candidates: list[dict]
    # CEO 결정
    ceo_decision_1: Optional[dict]   # 아이템 선택
    ceo_decision_2: Optional[dict]   # 기획서 승인
    selected_candidate: Optional[dict]
    # Planner 산출물
    prd: Optional[dict]
    architecture: Optional[dict]
    phase_plan: Optional[list]
    project_plan: Optional[dict]
    # 토론 루프
    debate_round: int
    debate_history: list[dict]
    consensus_reached: bool
    # TaskSpec 변환 결과
    task_specs: list[dict]
    # 상태
    status: str


# ─── conditional_edges 함수 ──────────────────────────────────────────────────


def should_continue_debate(state: IdeationState) -> str:
    """합의/조정/에스컬레이션 3경로 분기."""
    if state.get("consensus_reached"):
        return "write_prd"
    if state.get("debate_round", 0) >= 3:
        return "escalate_to_ceo"
    return "next_debate_round"


# ─── 노드 1: strategist_research ─────────────────────────────────────────────


async def strategist_research(state: IdeationState) -> IdeationState:
    """Strategist: 시장 데이터 수집 + 전략 분석."""
    from app.agents.strategist import collect_market_data, analyze_strategy, StrategyState

    logger.info("ideation_node_start", node="strategist_research", direction=state.get("direction"))

    strategy_state: StrategyState = {
        "direction": state.get("direction", "AI SaaS"),
        "budget": state.get("budget"),
        "timeline": state.get("timeline"),
        "search_results": state.get("search_results", []),
    }

    strategy_state = await collect_market_data(strategy_state)
    strategy_state = await analyze_strategy(strategy_state)

    logger.info("ideation_node_done", node="strategist_research",
                candidates=len(strategy_state.get("candidates", [])))

    return {
        **state,
        "search_results": strategy_state.get("search_results", []),
        "strategy_report": strategy_state.get("strategy_report"),
        "candidates": strategy_state.get("candidates", []),
        "status": "awaiting_ceo_item_selection",
    }


# ─── 노드 2: ceo_checkpoint_1 ────────────────────────────────────────────────


async def ceo_checkpoint_1(state: IdeationState) -> IdeationState:
    """HITL: CEO 아이템 선택 대기. interrupt()로 일시 중단."""
    try:
        from langgraph.types import interrupt
    except ImportError:
        from langgraph.errors import NodeInterrupt as interrupt  # type: ignore

    candidates = state.get("candidates", [])
    logger.info("ideation_node_start", node="ceo_checkpoint_1",
                candidates_count=len(candidates))

    # CEO에게 후보 목록 전달 후 선택 대기
    decision = interrupt({
        "type": "item_selection",
        "message": "아이템 후보를 선택하세요",
        "candidates": candidates,
        "strategy_report": state.get("strategy_report"),
    })

    # interrupt 재개 시 decision에 CEO 선택이 담겨있음
    selected = None
    if isinstance(decision, dict):
        selected_id = decision.get("selected_id")
        if selected_id:
            for c in candidates:
                if c.get("id") == selected_id:
                    selected = c
                    break
        if selected is None and decision.get("selected_candidate"):
            selected = decision.get("selected_candidate")

    if selected is None and candidates:
        # 기본값: 최고 점수 후보
        selected = max(candidates, key=lambda c: c.get("score", {}).get("total", 0))

    logger.info("ideation_node_done", node="ceo_checkpoint_1",
                selected_id=selected.get("id") if selected else None)

    return {
        **state,
        "ceo_decision_1": decision if isinstance(decision, dict) else {"auto_selected": True},
        "selected_candidate": selected,
        "debate_round": 0,
        "debate_history": [],
        "consensus_reached": False,
        "status": "item_selected",
    }


# ─── 노드 3: planner_evaluate ────────────────────────────────────────────────


async def planner_evaluate(state: IdeationState) -> IdeationState:
    """Planner: 선택 아이템 기술적 실현가능성 평가 + 토론 라운드 기록."""
    from app.agents.planner import evaluate_candidate, PlannerState

    debate_round = state.get("debate_round", 0) + 1
    debate_history = list(state.get("debate_history", []))

    logger.info("ideation_node_start", node="planner_evaluate", round=debate_round)

    planner_state: PlannerState = {
        "strategy_report": state.get("strategy_report", {}),
        "selected_candidate": state.get("selected_candidate", {}),
        "debate_round": debate_round - 1,
        "debate_history": debate_history,
    }

    eval_result = await evaluate_candidate(planner_state)

    feasible = eval_result.get("feasible", True)
    concerns = eval_result.get("concerns", [])
    suggestions = eval_result.get("suggestions", [])
    confidence = eval_result.get("confidence", 5)

    # 합의 기준: feasible=True, confidence >= 7 또는 이미 합의된 경우
    consensus = feasible and confidence >= 7

    debate_entry = {
        "round": debate_round,
        "type": "planner_evaluation",
        "strategist_message": {
            "candidate": state.get("selected_candidate", {}),
            "strategy_report_summary": {
                "direction": state.get("strategy_report", {}).get("direction", ""),
                "recommendation": state.get("strategy_report", {}).get("recommendation", ""),
            },
        },
        "planner_message": {
            "feasible": feasible,
            "concerns": concerns,
            "suggestions": suggestions,
            "confidence": confidence,
        },
        "consensus_reached": consensus,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    debate_history.append(debate_entry)

    # DB 기록
    await _record_debate_log(
        project_id=state.get("strategy_report", {}).get("project_id"),
        round_number=debate_round,
        strategist_message=debate_entry["strategist_message"],
        planner_message=debate_entry["planner_message"],
        consensus_reached=consensus,
        escalated=False,
    )

    logger.info("ideation_node_done", node="planner_evaluate",
                round=debate_round, consensus=consensus, feasible=feasible)

    return {
        **state,
        "debate_round": debate_round,
        "debate_history": debate_history,
        "consensus_reached": consensus,
        "status": "debate_in_progress" if not consensus else "consensus_reached",
    }


# ─── 노드 4: strategist_revise ───────────────────────────────────────────────


async def strategist_revise(state: IdeationState) -> IdeationState:
    """Strategist: Planner 피드백 반영 — 아이템 수정."""
    logger.info("ideation_node_start", node="strategist_revise",
                round=state.get("debate_round"))

    debate_history = state.get("debate_history", [])
    selected_candidate = dict(state.get("selected_candidate", {}))
    strategy_report = state.get("strategy_report", {})

    # 최근 Planner 피드백 수집
    recent_concerns: list[str] = []
    recent_suggestions: list[str] = []
    for entry in reversed(debate_history):
        pm = entry.get("planner_message", {})
        recent_concerns = pm.get("concerns", [])
        recent_suggestions = pm.get("suggestions", [])
        break

    # LLM으로 아이템 수정 (또는 fallback)
    try:
        from app.services.model_router import get_llm_for_agent
        from langchain_core.messages import HumanMessage, SystemMessage

        llm, _ = get_llm_for_agent("strategist_analyze")

        system_prompt = """당신은 Business Strategist입니다. Planner의 기술적 피드백을 반영해 아이템을 개선하세요.
기존 아이템 JSON에서 risks, competitive_edge, mvp_timeline, mvp_cost를 수정하고
수정된 아이템 JSON만 출력하세요."""

        user_message = f"""기존 아이템:
{json.dumps(selected_candidate, ensure_ascii=False, indent=2)}

Planner 우려사항: {json.dumps(recent_concerns, ensure_ascii=False)}
Planner 제안: {json.dumps(recent_suggestions, ensure_ascii=False)}

위 피드백을 반영해 개선된 아이템 JSON을 작성하세요."""

        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw = response.content if hasattr(response, "content") else str(response)
        json_str = raw.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()
        revised = json.loads(json_str)
        # 기존 필드 보존하면서 수정
        selected_candidate.update(revised)
    except Exception as e:
        logger.warning("strategist_revise_llm_failed", error=str(e))
        # Fallback: concerns 기반 risks 업데이트
        if recent_concerns:
            selected_candidate["risks"] = recent_concerns[:3]
        if recent_suggestions:
            selected_candidate["competitive_edge"] = (
                selected_candidate.get("competitive_edge", "") +
                f" [개선: {recent_suggestions[0]}]"
            )

    # 토론 이력에 수정 기록 추가
    debate_history = list(state.get("debate_history", []))
    debate_history.append({
        "round": state.get("debate_round"),
        "type": "strategist_revision",
        "revision_based_on": recent_concerns,
        "revised_candidate_id": selected_candidate.get("id"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    logger.info("ideation_node_done", node="strategist_revise",
                candidate_id=selected_candidate.get("id"))

    return {
        **state,
        "selected_candidate": selected_candidate,
        "debate_history": debate_history,
        "status": "candidate_revised",
    }


# ─── 노드 5: planner_write_prd ───────────────────────────────────────────────


async def planner_write_prd(state: IdeationState) -> IdeationState:
    """Planner: PRD + Architecture + Phase Plan + ProjectPlan 조립."""
    from app.agents.planner import (
        write_prd,
        design_architecture,
        create_phase_plan,
        assemble_project_plan,
        PlannerState,
    )

    logger.info("ideation_node_start", node="planner_write_prd")

    planner_state: PlannerState = {
        "strategy_report": state.get("strategy_report", {}),
        "selected_candidate": state.get("selected_candidate", {}),
        "prd": state.get("prd"),
        "architecture": state.get("architecture"),
        "phase_plan": state.get("phase_plan"),
        "project_plan": state.get("project_plan"),
        "debate_round": state.get("debate_round", 0),
        "debate_history": state.get("debate_history", []),
        "consensus_reached": state.get("consensus_reached", True),
    }

    planner_state = await write_prd(planner_state)
    planner_state = await design_architecture(planner_state)
    planner_state = await create_phase_plan(planner_state)
    planner_state = await assemble_project_plan(planner_state)

    logger.info("ideation_node_done", node="planner_write_prd",
                prd_features=len(planner_state.get("prd", {}).get("feature_list", [])))

    return {
        **state,
        "prd": planner_state.get("prd"),
        "architecture": planner_state.get("architecture"),
        "phase_plan": planner_state.get("phase_plan"),
        "project_plan": planner_state.get("project_plan"),
        "status": "awaiting_ceo_prd_approval",
    }


# ─── 노드 6: ceo_checkpoint_2 ────────────────────────────────────────────────


async def ceo_checkpoint_2(state: IdeationState) -> IdeationState:
    """HITL: CEO 기획서 승인 대기. interrupt()로 일시 중단."""
    try:
        from langgraph.types import interrupt
    except ImportError:
        from langgraph.errors import NodeInterrupt as interrupt  # type: ignore

    logger.info("ideation_node_start", node="ceo_checkpoint_2")

    project_plan = state.get("project_plan", {})

    decision = interrupt({
        "type": "prd_approval",
        "message": "기획서를 검토하고 승인 여부를 결정하세요",
        "project_plan": project_plan,
        "selected_candidate": state.get("selected_candidate"),
        "debate_summary": {
            "rounds": state.get("debate_round", 0),
            "consensus_reached": state.get("consensus_reached", False),
            "history_count": len(state.get("debate_history", [])),
        },
    })

    logger.info("ideation_node_done", node="ceo_checkpoint_2",
                approved=isinstance(decision, dict) and decision.get("approved", True))

    return {
        **state,
        "ceo_decision_2": decision if isinstance(decision, dict) else {"approved": True},
        "status": "prd_approved",
    }


# ─── 노드 7: escalate_to_ceo ─────────────────────────────────────────────────


async def escalate_to_ceo(state: IdeationState) -> IdeationState:
    """미수렴 시 양측 의견 병기하여 CEO에게 에스컬레이션. interrupt()."""
    try:
        from langgraph.types import interrupt
    except ImportError:
        from langgraph.errors import NodeInterrupt as interrupt  # type: ignore

    debate_history = state.get("debate_history", [])
    logger.info("ideation_node_start", node="escalate_to_ceo",
                rounds=state.get("debate_round", 0))

    # 양측 최종 의견 수집
    strategist_positions: list[dict] = []
    planner_positions: list[dict] = []
    for entry in debate_history:
        if entry.get("type") == "planner_evaluation":
            strategist_positions.append(entry.get("strategist_message", {}))
            planner_positions.append(entry.get("planner_message", {}))

    # DB에 에스컬레이션 기록
    await _record_debate_log(
        project_id=state.get("strategy_report", {}).get("project_id"),
        round_number=state.get("debate_round", 0),
        strategist_message=strategist_positions[-1] if strategist_positions else {},
        planner_message=planner_positions[-1] if planner_positions else {},
        consensus_reached=False,
        escalated=True,
    )

    decision = interrupt({
        "type": "escalation",
        "message": f"3라운드 토론 미수렴. CEO 판단이 필요합니다.",
        "debate_rounds": state.get("debate_round", 0),
        "strategist_final_position": strategist_positions[-1] if strategist_positions else {},
        "planner_final_position": planner_positions[-1] if planner_positions else {},
        "selected_candidate": state.get("selected_candidate"),
        "candidates": state.get("candidates", []),
    })

    # CEO 결정 반영
    new_state = {**state, "status": "escalated"}
    if isinstance(decision, dict):
        if "selected_candidate" in decision:
            new_state["selected_candidate"] = decision["selected_candidate"]
        if "consensus_reached" in decision:
            new_state["consensus_reached"] = decision["consensus_reached"]
        else:
            # CEO가 결정했으므로 합의로 처리
            new_state["consensus_reached"] = True
    else:
        new_state["consensus_reached"] = True

    logger.info("ideation_node_done", node="escalate_to_ceo")
    return new_state


# ─── 노드 8: convert_to_taskspecs ────────────────────────────────────────────


async def convert_to_taskspecs(state: IdeationState) -> IdeationState:
    """ProjectPlan → TaskSpec[] 변환."""
    logger.info("ideation_node_start", node="convert_to_taskspecs")

    project_plan = state.get("project_plan", {})
    phase_plan = project_plan.get("phase_plan", state.get("phase_plan", []))
    selected_candidate = state.get("selected_candidate", {})

    task_specs: list[dict] = []

    for phase in phase_plan:
        phase_num = phase.get("phase_number", 1)
        phase_name = phase.get("name", f"Phase {phase_num}")
        features = phase.get("key_features", [])
        deliverables = phase.get("deliverables", [])
        duration = phase.get("estimated_duration", "")
        cost = phase.get("estimated_cost", "")

        # Phase별 기능을 TaskSpec으로 변환
        for i, feature in enumerate(features):
            task_spec = {
                "task_id": f"T{phase_num:02d}{i+1:02d}",
                "title": feature,
                "description": f"[{phase_name}] {feature} 구현",
                "phase": phase_num,
                "phase_name": phase_name,
                "priority": "must" if phase_num == 1 else "should" if phase_num == 2 else "could",
                "estimated_duration": duration,
                "estimated_cost": cost,
                "dependencies": [f"T{phase_num:02d}{j+1:02d}" for j in range(i)] if i > 0 else [],
                "deliverables": deliverables,
                "candidate_id": selected_candidate.get("id", ""),
                "candidate_title": selected_candidate.get("title", ""),
            }
            task_specs.append(task_spec)

        # Phase 완료 태스크 추가
        task_specs.append({
            "task_id": f"T{phase_num:02d}99",
            "title": f"{phase_name} 완료 검증",
            "description": f"[{phase_name}] 산출물 검증 및 CEO 리뷰",
            "phase": phase_num,
            "phase_name": phase_name,
            "priority": "must",
            "estimated_duration": "1주",
            "estimated_cost": "",
            "dependencies": [f"T{phase_num:02d}{j+1:02d}" for j in range(len(features))],
            "deliverables": deliverables,
            "candidate_id": selected_candidate.get("id", ""),
            "candidate_title": selected_candidate.get("title", ""),
            "type": "milestone",
        })

    logger.info("ideation_node_done", node="convert_to_taskspecs",
                task_specs_count=len(task_specs))

    return {
        **state,
        "task_specs": task_specs,
        "status": "completed",
    }


# ─── DB 헬퍼 ─────────────────────────────────────────────────────────────────


async def _record_debate_log(
    project_id,
    round_number: int,
    strategist_message: dict,
    planner_message: dict,
    consensus_reached: bool,
    escalated: bool,
) -> None:
    """debate_logs 테이블에 토론 이력 기록."""
    import asyncpg
    from app.config import settings

    db_url = getattr(settings, "DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
    if not db_url:
        logger.warning("record_debate_log_no_db_url")
        return

    try:
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            await conn.execute(
                """
                INSERT INTO debate_logs
                    (project_id, round_number, strategist_message,
                     planner_message, consensus_reached, escalated)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
                """,
                str(project_id) if project_id else None,
                round_number,
                json.dumps(strategist_message, ensure_ascii=False),
                json.dumps(planner_message, ensure_ascii=False),
                consensus_reached,
                escalated,
            )
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("record_debate_log_failed", error=str(e))


# ─── 서브그래프 빌더 ─────────────────────────────────────────────────────────


def build_ideation_subgraph(checkpointer=None):
    """
    Ideation 서브그래프 컴파일.
    checkpointer: PostgresSaver 인스턴스 (없으면 MemorySaver fallback)
    """
    from langgraph.graph import StateGraph, END, START

    builder = StateGraph(IdeationState)

    # 노드 등록
    builder.add_node("strategist_research", strategist_research)
    builder.add_node("ceo_checkpoint_1", ceo_checkpoint_1)
    builder.add_node("planner_evaluate", planner_evaluate)
    builder.add_node("strategist_revise", strategist_revise)
    builder.add_node("planner_write_prd", planner_write_prd)
    builder.add_node("ceo_checkpoint_2", ceo_checkpoint_2)
    builder.add_node("escalate_to_ceo", escalate_to_ceo)
    builder.add_node("convert_to_taskspecs", convert_to_taskspecs)

    # 엣지 등록
    builder.add_edge(START, "strategist_research")
    builder.add_edge("strategist_research", "ceo_checkpoint_1")
    builder.add_edge("ceo_checkpoint_1", "planner_evaluate")

    # conditional_edges: 합의/조정/에스컬레이션
    builder.add_conditional_edges(
        "planner_evaluate",
        should_continue_debate,
        {
            "write_prd": "planner_write_prd",
            "next_debate_round": "strategist_revise",
            "escalate_to_ceo": "escalate_to_ceo",
        },
    )

    builder.add_edge("strategist_revise", "planner_evaluate")
    builder.add_edge("planner_write_prd", "ceo_checkpoint_2")
    builder.add_edge("ceo_checkpoint_2", "convert_to_taskspecs")
    builder.add_edge("escalate_to_ceo", "planner_evaluate")
    builder.add_edge("convert_to_taskspecs", END)

    compile_kwargs: dict = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer

    graph = builder.compile(**compile_kwargs)
    return graph
