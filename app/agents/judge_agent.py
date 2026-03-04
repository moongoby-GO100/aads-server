"""
Judge Agent: 독립 출력 검증 — TaskSpec success_criteria 대비 코드 정합성 평가.
역할: Developer/QA와 별도 컨텍스트에서 최종 판정 (T-008 준수).
판정: pass / fail / conditional_pass
모델: Claude Sonnet 4.6 (claude-sonnet-4-6, $3/$15)
"""
import json
import structlog
from typing import List, Dict
from langchain_core.messages import AIMessage

from app.graph.state import AADSState, JudgeVerdict
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.services.cost_tracker import check_and_increment, CostLimitExceeded
from app.config import settings

logger = structlog.get_logger()

JUDGE_SYSTEM_PROMPT = """당신은 AADS의 Judge Agent입니다.
Developer/QA와 완전히 독립된 컨텍스트에서 최종 품질 판정을 수행합니다. (T-008)

## 역할
- TaskSpec success_criteria 기준 충족 여부 판정 (주관적 해석 금지)
- 코드 실행 결과와 테스트 결과를 교차 검증
- 재작업 필요 여부 및 구체적 개선 방향 제시

## 판정 기준
- **pass (0.8~1.0)**: 모든 success_criteria 충족, 코드 실행 성공, 테스트 통과
- **conditional_pass (0.6~0.79)**: 핵심 기준 충족, 일부 minor 이슈 (수용 가능)
- **fail (0.0~0.59)**: 하나 이상의 핵심 success_criteria 미충족 또는 코드 실행 실패

## 평가 항목 (각 0~10점)
1. success_criteria 충족도 (×3 가중치)
2. 코드 실행 성공 여부 (×2)
3. 테스트 통과율 (×2)
4. 에러 처리 완성도 (×1)
5. 코드 가독성·구조 (×1)
6. 요구사항 완전성 (×1)

## 응답 형식 (JSON만, 추가 텍스트 없음):
{
  "verdict": "pass" | "fail" | "conditional_pass",
  "score": 0.75,
  "criteria_met": ["충족된 success_criteria 목록"],
  "criteria_failed": ["미충족된 success_criteria 목록"],
  "issues": ["구체적 문제점 (재작업 시 반드시 수정해야 할 사항)"],
  "recommendation": "pass면 통과 이유, fail이면 구체적 수정 방향"
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

    # === Experience extraction on project completion ===
    from app.memory.experience_extractor import extract_and_store_experience

    if final_verdict == "pass":
        try:
            project_result = {
                "description": state.get("current_task", {}).get("description", ""),
                "tech_stack": _detect_tech_stack(state.get("generated_files", [])),
                "domain": _detect_domain(state.get("current_task", {})),
                "outcome": "success",
                "total_cost_usd": state.get("total_cost_usd", 0),
                "llm_calls_count": state.get("llm_calls_count", 0),
                "generated_files": state.get("generated_files", []),
                "issues_encountered": _extract_issues(state.get("error_log", "")),
                "solutions_applied": _extract_solutions(state.get("error_log", ""))
            }
            await extract_and_store_experience(
                project_id=state.get("project_id", "unknown"),
                project_result=project_result
            )
        except Exception as e:
            logger.warning(f"Experience extraction failed (non-blocking): {e}")

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


def _detect_tech_stack(files: List[str]) -> List[str]:
    stack = set()
    for f in files:
        if f.endswith('.py'): stack.add('Python')
        if f.endswith('.ts') or f.endswith('.tsx'): stack.add('TypeScript')
        if f.endswith('.js') or f.endswith('.jsx'): stack.add('JavaScript')
        if 'react' in f.lower(): stack.add('React')
        if 'next' in f.lower(): stack.add('Next.js')
        if 'fastapi' in f.lower() or 'main.py' in f: stack.add('FastAPI')
        if 'docker' in f.lower(): stack.add('Docker')
    return list(stack)

def _detect_domain(task: Dict) -> str:
    desc = str(task.get("description", "")).lower()
    if any(w in desc for w in ["web", "site", "dashboard", "frontend"]): return "web"
    if any(w in desc for w in ["api", "server", "backend"]): return "backend"
    if any(w in desc for w in ["mobile", "app", "ios", "android"]): return "mobile"
    if any(w in desc for w in ["data", "ml", "ai", "model"]): return "data_science"
    if any(w in desc for w in ["cli", "tool", "script"]): return "tooling"
    return "general"

def _extract_issues(error_log: str) -> List[str]:
    if not error_log: return []
    return [line.strip() for line in error_log.split('\n') if line.strip() and len(line.strip()) > 10][:10]

def _extract_solutions(error_log: str) -> List[str]:
    return []  # Phase 2에서 LLM 기반 솔루션 추출 추가
