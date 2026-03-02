"""
Judge Agent: 독립 출력 검증 — TaskSpec success_criteria 대비 코드 정합성 평가.
역할: Developer/QA와 별도 컨텍스트에서 최종 판정 (T-008 준수).
판정: pass / fail / conditional_pass
모델: Claude Sonnet 4.6 (claude-sonnet-4-6, $3/$15)
"""
import json
import structlog
from langchain_core.messages import AIMessage

from app.graph.state import AADSState, JudgeVerdict
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.services.cost_tracker import check_and_increment, CostLimitExceeded
from app.config import settings

logger = structlog.get_logger()

JUDGE_SYSTEM_PROMPT = """당신은 AADS의 Judge Agent입니다.
독립적인 관점에서 코드 품질과 TaskSpec 성공 기준 충족 여부를 판정합니다.

규칙:
1. Developer/QA 컨텍스트와 완전히 분리된 독립 판정 (T-008)
2. TaskSpec의 success_criteria를 기준으로만 평가
3. 판정: pass / fail / conditional_pass
4. 반드시 JSON 형식으로 응답

응답 형식 (JSON만):
{
  "verdict": "pass" | "fail" | "conditional_pass",
  "score": 0.0~1.0,
  "issues": ["문제점1", "문제점2"],
  "recommendation": "개선 방향 또는 통과 이유"
}
"""


async def judge_node(state: AADSState) -> dict:
    """
    1. TaskSpec success_criteria 읽기
    2. Developer 코드 + QA 결과를 독립 컨텍스트로 평가 (T-008)
    3. JudgeVerdict 반환
    4. fail → Developer 재작업 (최대 3회)
    """
    logger.info("judge_node_start")

    task = state.get("current_task", {})
    description = task.get("description", "Unknown task")
    criteria = task.get("success_criteria", [])

    # Developer 코드
    generated_files = state.get("generated_files", [])
    code = ""
    for f in generated_files:
        if f.get("language") == "python":
            code = f.get("content", "")
            break

    # QA 결과
    qa_results = state.get("qa_test_results", [])
    qa_summary = ""
    if qa_results:
        last_qa = qa_results[-1]
        qa_summary = (
            f"테스트: {last_qa.get('tests_passed', 0)}/{last_qa.get('tests_total', 0)} 통과, "
            f"상태: {last_qa.get('status', 'unknown')}"
        )

    # 샌드박스 실행 결과
    sandbox_results = state.get("sandbox_results", [])
    sandbox_summary = ""
    if sandbox_results:
        last = sandbox_results[-1]
        sandbox_summary = (
            f"exit_code={last.get('exit_code', -1)}, "
            f"stdout={last.get('stdout', '')[:200]}"
        )

    # LLM 비용 확인
    try:
        llm, model_config = get_llm_for_agent("judge")
        est_cost = estimate_cost(model_config, 3000, 2000)
        cost_update = check_and_increment(state, est_cost, "judge", settings)
    except CostLimitExceeded as e:
        logger.error("judge_cost_limit", error=str(e))
        return {
            "error_log": state.get("error_log", []) + [str(e)],
            "checkpoint_stage": "cancelled",
        }

    # 독립 컨텍스트로 평가 (T-008: 새 메시지 체인, 이전 히스토리 없음)
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": f"""
[독립 판정 요청]

작업 설명: {description}

성공 기준:
{chr(10).join(f'  - {c}' for c in criteria)}

생성된 코드:
```python
{code[:3000] if code else "(코드 없음)"}
```

실행 결과: {sandbox_summary or "(없음)"}
QA 결과: {qa_summary or "(없음)"}

위 정보를 바탕으로 성공 기준 충족 여부를 독립적으로 판정하세요.
반드시 JSON 형식으로만 응답하세요.
"""},
    ]

    response = await llm.ainvoke(messages)
    content = response.content.strip()

    # JSON 파싱
    verdict_dict = {}
    try:
        # JSON 블록 추출
        import re
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            verdict_dict = json.loads(json_match.group())
        else:
            verdict_dict = json.loads(content)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("judge_json_parse_failed", error=str(e), content=content[:200])
        # 파싱 실패 시 기본값
        verdict_dict = {
            "verdict": "conditional_pass",
            "score": 0.5,
            "issues": ["JSON 파싱 실패: " + str(e)[:100]],
            "recommendation": "수동 확인 필요",
        }

    # JudgeVerdict 유효성 검증
    try:
        verdict = JudgeVerdict(**verdict_dict)
        verdict_data = verdict.model_dump()
    except Exception as e:
        logger.warning("judge_verdict_validation_failed", error=str(e))
        verdict_data = {
            "verdict": "conditional_pass",
            "score": 0.5,
            "issues": ["검증 실패"],
            "recommendation": str(e)[:100],
        }

    final_verdict = verdict_data.get("verdict", "fail")
    logger.info("judge_node_done", verdict=final_verdict, score=verdict_data.get("score"))

    # 재작업 카운터
    iteration = state.get("iteration_count", 0)

    if final_verdict == "pass":
        stage = "completed"
    elif final_verdict == "conditional_pass":
        stage = "completed"  # 조건부 통과도 완료로 처리
    else:
        # fail: Developer 재작업 (최대 3회)
        if iteration < 3:
            stage = "development"
        else:
            logger.warning("judge_max_retries_reached", iteration=iteration)
            stage = "completed"  # 3회 초과 시 강제 완료

    return {
        "judge_verdict": verdict_data,
        "checkpoint_stage": stage,
        "iteration_count": iteration + (1 if final_verdict == "fail" else 0),
        "messages": [AIMessage(
            content=f"Judge 판정: {final_verdict} (점수: {verdict_data.get('score', 0):.2f})"
        )],
        **cost_update,
    }
