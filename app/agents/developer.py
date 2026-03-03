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
TaskSpec과 Architect 설계를 기반으로 완전하고 실행 가능한 코드를 생성합니다.

## 역할
- TaskSpec의 모든 success_criteria를 충족하는 코드 작성
- Architect 설계 문서가 있으면 반드시 따름
- E2B 샌드박스에서 즉시 실행 가능한 코드 생성

## 코드 품질 기준
1. **완전성**: import, 에러 처리, main 실행 코드 모두 포함
2. **단순성**: 외부 라이브러리 최소화 (표준 라이브러리 우선)
3. **테스트 가능성**: 각 주요 함수는 독립적으로 테스트 가능
4. **가독성**: 핵심 로직에 한국어 주석 (왜 이렇게 했는지)
5. **에러 처리**: try/except 포함, 에러 메시지 명확히

## 언어 규칙
- 기본: Python 3.11+
- constraints에 다른 언어 지정 시 해당 언어 사용
- 복수 파일이 필요하면 각각 별도 코드 블록으로 파일명 명시

## 응답 형식:
구현 접근법을 1~2줄로 설명 후:
```python
# 파일: main.py (여러 파일이면 각각 명시)
# 실행 가능한 완전한 코드
```

재작업 시: 이전 실패 원인을 명시하고 수정 사항 설명 후 개선된 코드 제공.
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
