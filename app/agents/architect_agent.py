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
TaskSpec을 받아 시스템 설계 문서를 생성합니다.

출력 형식 (JSON 블록):
```json
{
  "db_schema": "테이블 정의 또는 NoSQL 스키마",
  "api_structure": "엔드포인트 목록 및 스펙",
  "file_structure": "프로젝트 디렉토리 구조",
  "tech_stack": ["사용 기술 목록"],
  "implementation_notes": "개발자에게 전달할 구현 지침"
}
```

규칙:
1. TaskSpec의 success_criteria를 모두 충족하는 최소 설계
2. Python/FastAPI 기반 구현 가정 (Phase 1)
3. E2B 샌드박스 실행 가능한 범위로 한정
4. JSON 블록만 출력 (설명 최소화)
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
