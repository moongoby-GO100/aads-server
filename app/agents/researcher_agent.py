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
Supervisor 요청 시 기술 조사, 라이브러리 비교, 구현 방법 탐색을 수행합니다.

## 역할
- 최적의 기술 스택 및 라이브러리 선택 지원
- 구현 접근법 비교 분석
- 잠재적 기술 리스크 사전 식별
- Developer에게 즉시 활용 가능한 코드 패턴 제공

## 조사 방법론
1. **핵심 요구사항 파악**: 조사 요청의 핵심 기술 문제 식별
2. **옵션 비교**: 최소 2개 이상 접근법 비교 (장단점)
3. **권장 선택**: 제약 조건 고려한 최적 옵션 추천 근거 포함
4. **즉시 사용 코드**: Developer가 복사-붙여넣기로 활용 가능한 코드 스니펫

## 출력 형식:

### 핵심 발견
- [발견 1]
- [발견 2]
- [발견 3]

### 옵션 비교
| 옵션 | 장점 | 단점 | 적합도 |
|------|------|------|--------|
| A    | ...  | ...  | ★★★   |
| B    | ...  | ...  | ★★    |

### 권장 선택
**[권장 옵션]**: [선택 이유]

### 구현 코드 스니펫
```python
# 즉시 활용 가능한 핵심 코드 (30줄 이내)
```

### 주의사항
- [불확실한 정보나 주의해야 할 점]

규칙: 불확실한 정보는 반드시 "[불확실]" 태그로 명시.
"""


async def researcher_node(state: AADSState) -> dict:
    """
    1. 현재 태스크 기반 기술 조사 (LLM)
    2. 리서치 결과를 state에 추가
    """
    logger.info("researcher_node_start")

    # === Search past experiences for similar projects ===
    from app.memory.store import memory_store

    try:
        async with memory_store.pool.acquire() as conn:
            # 텍스트 기반 검색 (Phase 2에서 벡터 검색으로 업그레이드)
            task_desc = state.get("current_task", {}).get("description", "")
            if task_desc:
                rows = await conn.fetch("""
                    SELECT content, experience_type, domain, rif_score
                    FROM experience_memory
                    WHERE content::text ILIKE $1
                    ORDER BY rif_score DESC, created_at DESC
                    LIMIT 5
                """, f"%{task_desc[:50]}%")

                if rows:
                    past_experiences = [dict(r) for r in rows]
                    state["research_results"] = state.get("research_results", []) + [{
                        "source": "experience_memory",
                        "type": "past_experiences",
                        "data": past_experiences,
                        "count": len(past_experiences)
                    }]
                    logger.info(f"Found {len(past_experiences)} relevant past experiences")
    except Exception as e:
        logger.warning(f"Experience search failed (non-blocking): {e}")

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
