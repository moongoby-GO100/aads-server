"""
Researcher Agent: 기술 조사 + 라이브러리 검색.
Supervisor 요청 시 온디맨드 호출.
"""
import structlog
from app.graph.state import AADSState
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.services.cost_tracker import check_and_increment, CostLimitExceeded
from app.config import settings

logger = structlog.get_logger()

RESEARCHER_SYSTEM_PROMPT = """당신은 AADS의 Researcher Agent입니다.
기술 조사, 라이브러리 검색, 문서 참조를 수행합니다.

출력 형식:
1. 핵심 발견 (3~5개 bullet)
2. 권장 라이브러리/도구
3. 구현 참고 링크 (실제 URL 대신 문서명으로)
4. 개발자에게 전달할 핵심 인사이트

규칙:
1. 간결하고 실행 가능한 정보 우선
2. Phase 1 범위 (Python 코드 생성)에 맞춤
3. 불확실한 정보는 명시적으로 표시
"""


async def researcher_node(state: AADSState) -> dict:
    """
    1. 현재 태스크 기반 기술 조사 (LLM)
    2. 리서치 결과를 state에 추가
    """
    logger.info("researcher_node_start")

    task = state.get("current_task", {})
    description = task.get("description", "Unknown task")
    research_query = task.get("research_query", description)

    try:
        llm, model_config = get_llm_for_agent("researcher")
        est_cost = estimate_cost(model_config, 2000, 4000)
        cost_update = check_and_increment(state, est_cost, "researcher", settings)
    except CostLimitExceeded as e:
        logger.error("researcher_cost_limit", error=str(e))
        return {
            "error_log": state.get("error_log", []) + [str(e)],
        }

    messages = [
        {"role": "system", "content": RESEARCHER_SYSTEM_PROMPT},
        {"role": "user", "content": f"""
조사 요청: {research_query}
태스크 컨텍스트: {description}
성공 기준: {", ".join(task.get("success_criteria", []))}

위 태스크를 구현하기 위한 기술 조사를 수행하세요.
"""},
    ]

    response = await llm.ainvoke(messages)
    research_result = {
        "query": research_query,
        "findings": response.content,
        "agent": "researcher",
    }

    existing_research = state.get("research_results", [])
    logger.info("researcher_node_done", query=research_query)
    return {
        **cost_update,
        "research_results": existing_research + [research_result],
    }
