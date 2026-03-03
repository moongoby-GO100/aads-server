"""
PM Agent: 사용자 요청 → 구조화 JSON TaskSpec 생성.
⚠️ interrupt()와 LLM 호출을 분리하여 비용 중복 방지.
⚠️ interrupt 전 side effect 없음 (멱등성 보장).
"""
import json
import structlog
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import interrupt

from app.graph.state import AADSState, TaskSpec
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.services.cost_tracker import check_and_increment
from app.config import settings

logger = structlog.get_logger()

PM_SYSTEM_PROMPT = """당신은 AADS의 PM (Project Manager) Agent입니다.
사용자의 요청을 분석하여 구조화된 TaskSpec JSON을 생성합니다.

## 역할
- 요구사항 명확화 및 범위 정의
- 기술 가능성 사전 검토
- 성공 기준을 검증 가능한 형태로 구체화
- 구현 우선순위 및 제약 조건 설정

## 규칙
1. success_criteria: 각 항목은 "~해야 한다" 형태로, 테스트 코드로 검증 가능해야 함
2. constraints: 언어/프레임워크 제약, 금지 외부 라이브러리, 성능 요구사항 포함
3. assigned_agent: "developer" (기본), 설계 복잡도 높으면 "architect" 우선
4. max_llm_calls: 반드시 15 이하 (R-012 준수)
5. budget_limit_usd: 작업 복잡도 기반 — 단순 10, 중간 25, 복잡 50
6. 모호한 요구사항은 가장 실용적인 해석으로 정의하고 assumptions에 기록

## 응답 형식 (JSON만 출력, 추가 설명 없음):
{
  "description": "작업을 한 문장으로 명확히 설명",
  "assigned_agent": "developer",
  "success_criteria": [
    "구체적이고 테스트 가능한 기준 1",
    "구체적이고 테스트 가능한 기준 2"
  ],
  "constraints": [
    "Python 3.11 이상 사용",
    "외부 라이브러리 최소화"
  ],
  "input_artifacts": [],
  "output_artifacts": ["main.py", "README.md"],
  "max_iterations": 3,
  "max_llm_calls": 10,
  "budget_limit_usd": 10.0,
  "priority": "high",
  "assumptions": ["사용자가 명시하지 않은 가정사항"],
  "status": "pending"
}
"""


async def pm_requirements_node(state: AADSState) -> dict:
    """
    PM이 요구사항을 분석하고 TaskSpec을 생성.
    interrupt()로 사용자 승인 대기.
    """
    logger.info("pm_node_start", stage="requirements")

    # 1. LLM 호출하여 TaskSpec 생성
    llm, model_config = get_llm_for_agent("pm")

    # 비용 사전 추정 (~3K input, ~2K output)
    est_cost = estimate_cost(model_config, 3000, 2000)
    cost_update = check_and_increment(state, est_cost, "pm", settings)

    # 메시지 변환 (LangChain 메시지 → dict)
    chat_messages = [{"role": "system", "content": PM_SYSTEM_PROMPT}]
    for m in state.get("messages", []):
        if hasattr(m, "type"):
            role = "user" if m.type in ("human", "user") else "assistant"
        else:
            role = "user"
        content = m.content if hasattr(m, "content") else str(m)
        chat_messages.append({"role": role, "content": content})

    response = await llm.ainvoke(chat_messages)

    # 2. TaskSpec 파싱
    try:
        # JSON 블록 추출 시도
        content = response.content
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        task_data = json.loads(content)
        task_spec = TaskSpec(**task_data)
    except (json.JSONDecodeError, Exception) as e:
        logger.error("taskspec_parse_failed", error=str(e))
        last_msg = state.get("messages", [])
        last_content = last_msg[-1].content if last_msg and hasattr(last_msg[-1], "content") else "Unknown request"
        task_spec = TaskSpec(
            description=last_content,
            assigned_agent="developer",
            success_criteria=["코드가 생성되어야 함"],
        )

    # 3. interrupt: 사용자에게 TaskSpec 승인 요청
    # ⚠️ interrupt 전에 side effect 없음 (멱등성)
    # ⚠️ try/except로 감싸지 않음 (LangGraph 규칙)
    approval = interrupt({
        "stage": "requirements",
        "message": "아래 작업 사양을 확인해주세요. 승인하시겠습니까?",
        "task_spec": task_spec.model_dump(),
    })

    # 4. 승인 처리 (resume 후 이 지점부터 실행)
    if approval is True or (isinstance(approval, dict) and approval.get("approved")):
        logger.info("pm_requirements_approved")
        return {
            "current_task": task_spec.model_dump(),
            "checkpoint_stage": "plan_review",
            "approved_stages": state.get("approved_stages", []) + ["requirements"],
            "messages": [AIMessage(content=f"요구사항이 승인되었습니다. TaskSpec: {task_spec.description}")],
            **cost_update,
        }
    else:
        # 수정 요청
        feedback = approval if isinstance(approval, str) else "수정 필요"
        logger.info("pm_requirements_revision", feedback=feedback)
        revision = state.get("revision_count", 0) + 1
        if revision >= 3:
            return {
                "checkpoint_stage": "cancelled",
                "error_log": state.get("error_log", []) + ["Max revisions reached"],
                **cost_update,
            }
        return {
            "messages": [HumanMessage(content=f"수정 요청: {feedback}")],
            "revision_count": revision,
            **cost_update,
        }
