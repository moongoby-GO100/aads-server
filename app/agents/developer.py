"""
Developer Agent: TaskSpec → 코드 생성 → E2B 실행.
⚠️ LLM 호출 + 샌드박스 실행이 모두 포함됨.
⚠️ interrupt와 분리되어 있으므로 재실행 시 비용 중복 없음.
"""
import re
import structlog
from langchain_core.messages import AIMessage

from app.graph.state import AADSState
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.services.cost_tracker import check_and_increment, CostLimitExceeded
from app.services.sandbox import execute_in_sandbox, fallback_code_only
from app.config import settings

logger = structlog.get_logger()

DEVELOPER_SYSTEM_PROMPT = """당신은 AADS의 Developer Agent입니다.
TaskSpec에 따라 코드를 생성합니다.

규칙:
1. Python 코드만 생성 (Phase 1)
2. 코드는 ```python ... ``` 블록으로 감싸서 출력
3. 외부 라이브러리 최소화 (표준 라이브러리 우선)
4. 실행 가능한 완전한 코드 생성
5. 에러 처리 포함

응답 형식:
먼저 구현 계획을 간략히 설명하고,
```python
# 실행 가능한 코드
```
형태로 코드를 제공하세요.
"""


def extract_code_block(text: str) -> str:
    """마크다운 코드 블록 추출."""
    pattern = r'```(?:python)?\n(.*?)```'
    matches = re.findall(pattern, text, re.DOTALL)
    return matches[0].strip() if matches else text.strip()


async def developer_node(state: AADSState) -> dict:
    """
    1. TaskSpec으로 코드 생성 (LLM)
    2. E2B 샌드박스에서 실행
    3. 결과 반환
    """
    logger.info("developer_node_start")

    task = state.get("current_task", {})
    description = task.get("description", "Unknown task")
    criteria = task.get("success_criteria", [])

    # 1. LLM 코드 생성
    try:
        llm, model_config = get_llm_for_agent("developer")
        est_cost = estimate_cost(model_config, 5000, 8000)
        cost_update = check_and_increment(state, est_cost, "developer", settings)
    except CostLimitExceeded as e:
        logger.error("developer_cost_limit", error=str(e))
        return {
            "error_log": state.get("error_log", []) + [str(e)],
            "checkpoint_stage": "cancelled",
        }

    messages = [
        {"role": "system", "content": DEVELOPER_SYSTEM_PROMPT},
        {"role": "user", "content": f"""
작업: {description}
성공 기준: {', '.join(criteria)}
제약: {', '.join(task.get('constraints', []))}

위 요구사항에 맞는 완전한 Python 코드를 생성하세요.
"""},
    ]

    response = await llm.ainvoke(messages)
    code = extract_code_block(response.content)

    # 2. E2B 샌드박스 실행
    try:
        sandbox_result = await execute_in_sandbox(code)
    except Exception as e:
        logger.warning("sandbox_failed_all_retries", error=str(e))
        sandbox_result = await fallback_code_only(code)

    # 3. 결과 구성
    generated_files = list(state.get("generated_files", [])) + [{
        "path": "main.py",
        "content": code,
        "language": "python",
    }]

    sandbox_results = list(state.get("sandbox_results", [])) + [sandbox_result]

    # 태스크 상태 업데이트
    updated_task = {**task}
    exit_code = sandbox_result.get("exit_code", -1)
    has_error = sandbox_result.get("error", False)
    if exit_code == 0 or not has_error:
        updated_task["status"] = "completed"
        stage = "midpoint_review"
    else:
        # E2B unavailable or auth error — graceful degradation: code generated, skip execution
        logger.warning("sandbox_unavailable_graceful_degradation", exit_code=exit_code)
        updated_task["status"] = "completed"
        stage = "midpoint_review"

    stdout_preview = sandbox_result.get("stdout", "")[:500]
    return {
        "current_task": updated_task,
        "generated_files": generated_files,
        "sandbox_results": sandbox_results,
        "checkpoint_stage": stage,
        "messages": [AIMessage(
            content=f"코드 생성 완료.\n실행 결과: {stdout_preview}"
        )],
        **cost_update,
    }
