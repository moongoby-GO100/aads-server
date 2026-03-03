"""
Architect Agent: TaskSpec → 시스템 설계 → 아키텍처 문서 생성.
설계서 산출: DB 스키마, API 구조, 파일 구조, 화면 구성.
"""
import structlog
from app.graph.state import AADSState
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.services.cost_tracker import check_and_increment, CostLimitExceeded
from app.config import settings

logger = structlog.get_logger()

ARCHITECT_SYSTEM_PROMPT = """당신은 AADS의 Architect Agent입니다.
TaskSpec을 받아 최적의 시스템 설계 문서를 생성합니다.

## 역할
- TaskSpec success_criteria를 100% 충족하는 최소 설계
- 기술 스택 선정 및 근거 제시
- Developer가 바로 구현할 수 있는 상세 지침 작성
- E2B 샌드박스 실행 가능한 범위 판단

## 설계 원칙
1. YAGNI (You Aren't Gonna Need It) — 필요한 것만 설계
2. 테스트 가능성 우선 — 각 컴포넌트는 단독으로 테스트 가능해야 함
3. 기술 스택은 constraints에서 지정한 것 우선, 없으면 Python 표준 라이브러리 우선
4. 외부 의존성이 있으면 대체 방법(fallback) 반드시 포함

## 출력 형식 (JSON 블록만, 추가 설명 없음):
```json
{
  "db_schema": "테이블/컬렉션 정의 또는 '없음'",
  "api_structure": "엔드포인트 목록 또는 '없음' (예: GET /health -> 200 OK)",
  "file_structure": "src/\\n  main.py\\n  utils.py\\nREADME.md",
  "tech_stack": ["python:3.11", "fastapi:0.110+"],
  "entry_point": "python main.py 또는 uvicorn main:app --port 8000",
  "key_algorithms": ["핵심 알고리즘 또는 패턴 설명"],
  "implementation_notes": "Developer에게 전달할 핵심 구현 지침 (3~5줄)",
  "test_strategy": "QA Agent가 테스트할 방법 (예: pytest, 직접 함수 호출)"
}
```
"""


def extract_json_block(text: str) -> dict:
    """JSON 블록 추출."""
    import re, json
    pattern = r'```(?:json)?\n(.*?)```'
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        try:
            return json.loads(matches[0].strip())
        except Exception:
            pass
    return {"implementation_notes": text.strip(), "tech_stack": ["python"]}


async def architect_node(state: AADSState) -> dict:
    """
    1. TaskSpec으로 시스템 설계 (LLM)
    2. 설계 문서 반환
    """
    logger.info("architect_node_start")

    task = state.get("current_task", {})
    description = task.get("description", "Unknown task")
    criteria = task.get("success_criteria", [])

    try:
        llm, model_config = get_llm_for_agent("architect")
        est_cost = estimate_cost(model_config, 3000, 6000)
        cost_update = check_and_increment(state, est_cost, "architect", settings)
    except CostLimitExceeded as e:
        logger.error("architect_cost_limit", error=str(e))
        return {
            "error_log": state.get("error_log", []) + [str(e)],
            "checkpoint_stage": "cancelled",
        }

    messages = [
        {"role": "system", "content": ARCHITECT_SYSTEM_PROMPT},
        {"role": "user", "content": f"""
작업: {description}
성공 기준: {", ".join(criteria)}
제약: {", ".join(task.get("constraints", []))}

위 요구사항에 맞는 시스템 설계 문서를 JSON 형식으로 생성하세요.
"""},
    ]

    response = await llm.ainvoke(messages)
    design = extract_json_block(response.content)

    logger.info("architect_node_done", design_keys=list(design.keys()))
    return {
        **cost_update,
        "architect_design": design,
        "checkpoint_stage": "development",
    }
