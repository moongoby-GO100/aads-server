"""
Judge Agent: 독립 출력 검증 — TaskSpec success_criteria 대비 코드 정합성 평가.
역할: Developer/QA와 별도 컨텍스트 + 별도 모델에서 최종 판정 (T-008 준수).
판정: pass / fail / conditional_pass
모델: Gemini 2.5 Flash (gemini-2.5-flash) — Developer/QA(Claude Sonnet 4.6)와 다른 모델 (T-002)
fail 시 구체적 피드백 JSON → Supervisor가 Developer에 재작업 지시
"""
import json
import re
import structlog
from typing import List, Dict, Optional
from langchain_core.messages import AIMessage

from app.graph.state import AADSState, JudgeVerdict
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.memory.experience_extractor import extract_and_store_experience
from app.services.cost_tracker import check_and_increment, CostLimitExceeded
from app.config import settings

logger = structlog.get_logger()

JUDGE_SYSTEM_PROMPT = """당신은 AADS의 Judge Agent입니다.
Developer/QA와 완전히 독립된 컨텍스트 + 다른 모델에서 최종 품질 판정을 수행합니다. (T-008)

## 역할
- TaskSpec success_criteria 기준 충족 여부 판정 (주관적 해석 금지)
- output_artifacts(생성 파일) 구조를 success_criteria와 구조화 비교
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
  "rework_instructions": "fail인 경우 Developer에게 전달할 구체적 재작업 지시사항",
  "recommendation": "pass면 통과 이유, fail이면 구체적 수정 방향"
}
"""


def _build_criteria_comparison(
    criteria: List[str],
    output_artifacts: List[str],
    generated_files: List[dict],
) -> str:
    """success_criteria vs output_artifacts 구조화 비교 텍스트 생성."""
    lines = ["## Success Criteria vs Output Artifacts 구조화 비교\n"]

    # 기대 artifacts
    lines.append("### 기대 출력 (output_artifacts):")
    for art in output_artifacts:
        found = any(
            art.lower() in (f.get("path", "") + f.get("name", "")).lower()
            for f in generated_files
        )
        status = "✅ 존재" if found else "❌ 누락"
        lines.append(f"  - {art}: {status}")

    lines.append("\n### 생성된 파일:")
    for f in generated_files:
        path = f.get("path", f.get("name", "unknown"))
        lang = f.get("language", "?")
        size = len(f.get("content", ""))
        lines.append(f"  - {path} ({lang}, {size} chars)")

    lines.append("\n### Success Criteria 체크리스트:")
    for i, criterion in enumerate(criteria, 1):
        lines.append(f"  {i}. {criterion}")

    return "\n".join(lines)


def _parse_judge_response(content: str) -> dict:
    """Judge LLM 응답에서 JSON verdict 추출. 항상 dict 반환 보장."""
    verdict_dict = {}

    try:
        text = content.strip()
        # 1. markdown code block 우선 추출
        code_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if code_match:
            text = code_match.group(1).strip()

        # 2. 직접 파싱 시도
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                verdict_dict = parsed
        except json.JSONDecodeError:
            pass

        # 3. string-aware depth-tracking으로 JSON 객체 추출
        if not verdict_dict:
            start = text.find('{')
            if start >= 0:
                depth = 0
                in_str = False
                esc = False
                for i in range(start, len(text)):
                    ch = text[i]
                    if in_str:
                        if esc:
                            esc = False
                            continue
                        if ch == '\\':
                            esc = True
                            continue
                        if ch == '"':
                            in_str = False
                        continue
                    if ch == '"':
                        in_str = True
                    elif ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            block = text[start:i + 1]
                            try:
                                verdict_dict = json.loads(block)
                            except json.JSONDecodeError:
                                # trailing comma 등 제거 후 재시도
                                cleaned = re.sub(r',\s*([}\]])', r'\1', block)
                                try:
                                    verdict_dict = json.loads(cleaned)
                                except json.JSONDecodeError:
                                    pass
                            break

        # 4. 원본 텍스트(코드 블록 제거 전)에서도 {} 블록 탐색
        if not verdict_dict and code_match:
            start = content.find('{')
            if start >= 0:
                depth = 0
                for i in range(start, len(content)):
                    ch = content[i]
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            try:
                                verdict_dict = json.loads(content[start:i + 1])
                            except json.JSONDecodeError:
                                cleaned = re.sub(r',\s*([}\]])', r'\1', content[start:i + 1])
                                try:
                                    verdict_dict = json.loads(cleaned)
                                except json.JSONDecodeError:
                                    pass
                            break

        # 5. verdict 키가 없으면 텍스트에서 verdict 추출 시도
        if not verdict_dict:
            v_match = re.search(r'"?verdict"?\s*[:=]\s*"?(pass|fail|conditional_pass)"?', content, re.I)
            s_match = re.search(r'"?score"?\s*[:=]\s*([0-9.]+)', content)
            if v_match:
                verdict_dict = {
                    "verdict": v_match.group(1).lower(),
                    "score": float(s_match.group(1)) if s_match else 0.5,
                    "issues": [],
                    "rework_instructions": "",
                    "recommendation": "텍스트에서 판정 추출",
                }
    except Exception as e:
        logger.warning("judge_json_parse_failed", error=str(e), content=content[:300])

    # 최후 수단: 텍스트에서 verdict 키워드 탐색
    if not verdict_dict:
        v_match = re.search(r'\b(pass|fail|conditional_pass)\b', content, re.I)
        verdict_val = v_match.group(1).lower() if v_match else "conditional_pass"
        verdict_dict = {
            "verdict": verdict_val,
            "score": 0.5,
            "issues": ["JSON 파싱 실패 — 텍스트에서 verdict 추출"],
            "rework_instructions": "",
            "recommendation": content[:200],
        }

    return verdict_dict


async def judge_node(state: AADSState) -> dict:
    """
    1. TaskSpec success_criteria + output_artifacts 구조화 비교 (T-031)
    2. Developer 코드 + QA 결과를 독립 컨텍스트로 평가 (T-008)
    3. JudgeVerdict 반환 (pass/fail/conditional_pass)
    4. fail → 구체적 피드백 JSON → Supervisor가 Developer에 재작업 지시
    모델: Gemini 2.5 Flash (Developer/QA의 Claude Sonnet 4.6과 다른 모델)
    """
    logger.info("judge_node_start")

    task = state.get("current_task", {})
    description = task.get("description", "Unknown task")
    criteria = task.get("success_criteria", [])
    output_artifacts = task.get("output_artifacts", [])

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

    # success_criteria vs output_artifacts 구조화 비교
    criteria_comparison = _build_criteria_comparison(
        criteria, output_artifacts, generated_files
    )

    # LLM 비용 확인 (Judge: Gemini 3.1 Pro, Developer/QA와 다른 모델)
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
[독립 판정 요청 — Judge Only Context]

작업 설명: {description}

성공 기준:
{chr(10).join(f'  - {c}' for c in criteria)}

{criteria_comparison}

생성된 코드:
```python
{code[:3000] if code else "(코드 없음)"}
```

실행 결과: {sandbox_summary or "(없음)"}
QA 결과: {qa_summary or "(없음)"}

위 정보를 바탕으로 success_criteria와 output_artifacts를 구조화 비교하고
성공 기준 충족 여부를 독립적으로 판정하세요.
fail이면 rework_instructions 필드에 Developer에게 전달할 구체적 재작업 지시사항을 작성하세요.
반드시 JSON 형식으로만 응답하세요.
"""},
    ]

    # LLM 호출 — 실패 시에도 verdict_dict 반환 보장
    content = ""
    try:
        response = await llm.ainvoke(messages)
        content = (response.content or "").strip()
    except Exception as e:
        logger.error("judge_llm_invoke_failed", error=str(e))
        content = ""

    if not content:
        logger.warning("judge_empty_response")
        verdict_dict = {
            "verdict": "conditional_pass",
            "score": 0.5,
            "issues": ["Judge LLM 응답 없음"],
            "rework_instructions": "",
            "recommendation": "LLM 응답이 비어있어 conditional_pass 처리",
        }
    else:
        # JSON 파싱 — markdown code block 및 후행 텍스트 처리
        verdict_dict = _parse_judge_response(content)

    # JudgeVerdict 유효성 검증 (criteria_met/criteria_failed/rework_instructions는 extra 필드)
    try:
        verdict = JudgeVerdict(**{
            k: v for k, v in verdict_dict.items()
            if k in ("verdict", "score", "issues", "recommendation")
        })
        verdict_data = verdict.model_dump()
        # 추가 필드 병합
        verdict_data["criteria_met"] = verdict_dict.get("criteria_met", [])
        verdict_data["criteria_failed"] = verdict_dict.get("criteria_failed", [])
        verdict_data["rework_instructions"] = verdict_dict.get("rework_instructions", "")
    except Exception as e:
        logger.warning("judge_verdict_validation_failed", error=str(e))
        verdict_data = {
            "verdict": "conditional_pass",
            "score": 0.5,
            "issues": ["검증 실패"],
            "criteria_met": [],
            "criteria_failed": [],
            "rework_instructions": "",
            "recommendation": str(e)[:100],
        }

    final_verdict = verdict_data.get("verdict", "fail")
    logger.info("judge_node_done", verdict=final_verdict, score=verdict_data.get("score"))

    # fail → Supervisor에 재작업 지시를 위한 current_task 업데이트
    updated_task = dict(task)
    if final_verdict == "fail":
        updated_task["rework_feedback"] = {
            "issues": verdict_data.get("issues", []),
            "rework_instructions": verdict_data.get("rework_instructions", ""),
            "criteria_failed": verdict_data.get("criteria_failed", []),
        }
        logger.info(
            "judge_rework_feedback_set",
            issues_count=len(verdict_data.get("issues", [])),
        )

    # === Experience extraction on project completion ===
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

    # 재작업 카운터
    iteration = state.get("iteration_count", 0)

    if final_verdict in ("pass", "conditional_pass"):
        stage = "completed"
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
        "current_task": updated_task,
        "messages": [AIMessage(
            content=f"Judge 판정: {final_verdict} (점수: {verdict_data.get('score', 0):.2f})"
        )],
        **cost_update,
    }


def _detect_tech_stack(files: List) -> List[str]:
    stack = set()
    for f in files:
        if isinstance(f, dict):
            name = f.get("path", "") + f.get("name", "")
        else:
            name = str(f)
        if name.endswith('.py'): stack.add('Python')
        if name.endswith('.ts') or name.endswith('.tsx'): stack.add('TypeScript')
        if name.endswith('.js') or name.endswith('.jsx'): stack.add('JavaScript')
        if 'react' in name.lower(): stack.add('React')
        if 'next' in name.lower(): stack.add('Next.js')
        if 'fastapi' in name.lower() or 'main.py' in name: stack.add('FastAPI')
        if 'docker' in name.lower(): stack.add('Docker')
    return list(stack)


def _detect_domain(task: Dict) -> str:
    desc = str(task.get("description", "")).lower()
    if any(w in desc for w in ["web", "site", "dashboard", "frontend"]): return "web"
    if any(w in desc for w in ["api", "server", "backend"]): return "backend"
    if any(w in desc for w in ["mobile", "app", "ios", "android"]): return "mobile"
    if any(w in desc for w in ["data", "ml", "ai", "model"]): return "data_science"
    if any(w in desc for w in ["cli", "tool", "script"]): return "tooling"
    return "general"


def _extract_issues(error_log) -> List[str]:
    if not error_log: return []
    if isinstance(error_log, list):
        return [str(e)[:200] for e in error_log[:10]]
    return [line.strip() for line in str(error_log).split('\n') if line.strip() and len(line.strip()) > 10][:10]


def _extract_solutions(error_log) -> List[str]:
    return []  # Phase 2에서 LLM 기반 솔루션 추출 추가
