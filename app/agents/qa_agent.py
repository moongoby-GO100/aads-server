"""
QA Agent: Developer 산출물 검증 — 테스트 코드 생성 + E2B 실행.
역할: Developer 코드를 입력받아 테스트 케이스를 생성하고 샌드박스에서 실행.
모델: Claude Sonnet 4.6 (claude-sonnet-4-6, $3/$15)
"""
import json
import re
import structlog
from langchain_core.messages import AIMessage

from app.graph.state import AADSState
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.services.cost_tracker import check_and_increment, CostLimitExceeded
from app.services.sandbox import execute_in_sandbox, fallback_code_only
from app.config import settings

logger = structlog.get_logger()

QA_SYSTEM_PROMPT = """당신은 AADS의 QA Agent입니다.
Developer가 생성한 코드를 검증하는 테스트를 작성합니다.

규칙:
1. 주어진 성공 기준(success_criteria)을 모두 검증하는 테스트 작성
2. pytest 형식 사용 (test_ 접두사 함수)
3. 각 테스트는 독립적이어야 함
4. 테스트 코드는 ```python ... ``` 블록으로 감싸서 출력
5. 실행 가능한 완전한 테스트 코드 생성

응답 형식:
- 간략한 테스트 전략 설명
- ```python
  # 테스트 코드
  ``` 블록
"""


def extract_code_block(text: str) -> str:
    """마크다운 코드 블록 추출."""
    pattern = r'```(?:python)?\n(.*?)```'
    matches = re.findall(pattern, text, re.DOTALL)
    return matches[0].strip() if matches else text.strip()


async def qa_node(state: AADSState) -> dict:
    """
    1. Developer 코드 및 TaskSpec 읽기
    2. 테스트 코드 생성 (LLM)
    3. E2B 샌드박스에서 테스트 실행
    4. 구조화 결과 반환
    """
    logger.info("qa_node_start")

    task = state.get("current_task", {})
    description = task.get("description", "Unknown task")
    criteria = task.get("success_criteria", [])

    # Developer 코드 가져오기
    generated_files = state.get("generated_files", [])
    developer_code = ""
    for f in generated_files:
        if f.get("language") == "python":
            developer_code = f.get("content", "")
            break

    if not developer_code:
        logger.warning("qa_no_developer_code")
        return {
            "qa_test_results": [{
                "status": "skip",
                "reason": "Developer 코드 없음",
                "tests_passed": 0,
                "tests_failed": 0,
                "tests_total": 0,
            }],
            "checkpoint_stage": "final_review",
        }

    # LLM 비용 확인
    try:
        llm, model_config = get_llm_for_agent("qa")
        est_cost = estimate_cost(model_config, 4000, 6000)
        cost_update = check_and_increment(state, est_cost, "qa", settings)
    except CostLimitExceeded as e:
        logger.error("qa_cost_limit", error=str(e))
        return {
            "error_log": state.get("error_log", []) + [str(e)],
            "checkpoint_stage": "cancelled",
        }

    # 테스트 코드 생성
    messages = [
        {"role": "system", "content": QA_SYSTEM_PROMPT},
        {"role": "user", "content": f"""
작업 설명: {description}
성공 기준:
{chr(10).join(f'  - {c}' for c in criteria)}

Developer가 생성한 코드:
```python
{developer_code}
```

위 코드를 검증하는 pytest 테스트 코드를 작성하세요.
"""},
    ]

    response = await llm.ainvoke(messages)
    test_code = extract_code_block(response.content)

    # E2B 샌드박스에서 테스트 실행 (pytest 사용)
    # 테스트 파일에 원본 코드 + 테스트 코드를 합쳐서 실행
    combined_code = f"""
{developer_code}

{test_code}

# pytest 결과를 캡처하여 실행
import sys
import io

# 간단히 test_ 함수들을 직접 실행
import traceback
results = {{"passed": [], "failed": []}}
namespace = {{}}
exec(compile(open(__file__).read() if hasattr(open, "__file__") else "", "<test>", "exec"), namespace)

# 현재 네임스페이스에서 test_ 함수 수집
test_funcs = [name for name in dir() if name.startswith("test_")]
for fname in test_funcs:
    try:
        func = eval(fname)
        func()
        results["passed"].append(fname)
    except Exception as e:
        results["failed"].append({{"name": fname, "error": str(e)}})

print(f"PASSED: {{len(results['passed'])}}, FAILED: {{len(results['failed'])}}")
if results["failed"]:
    for f in results["failed"]:
        print(f"  FAIL: {{f['name']}}: {{f['error']}}")
"""

    # 단순하게 테스트 코드만 실행 (실용적 접근)
    test_runner = f"""
import sys, traceback, io

# 원본 코드
{developer_code}

# 테스트 코드
{test_code}

# 테스트 함수 찾아서 실행
import inspect
test_funcs = [
    (name, obj) for name, obj in list(locals().items())
    if name.startswith("test_") and callable(obj)
]

passed = []
failed = []
for name, func in test_funcs:
    try:
        func()
        passed.append(name)
    except Exception as e:
        failed.append((name, str(e)))

total = len(passed) + len(failed)
print(f"QA_RESULT: {{len(passed)}}/{{total}} passed")
for fname, err in failed:
    print(f"FAIL: {{fname}}: {{err}}")
if not passed and not failed:
    print("QA_RESULT: 0/0 passed (no test functions found)")
"""

    try:
        sandbox_result = await execute_in_sandbox(test_runner)
    except Exception as e:
        logger.warning("qa_sandbox_failed", error=str(e))
        sandbox_result = await fallback_code_only(test_runner)

    stdout = sandbox_result.get("stdout", "")
    exit_code = sandbox_result.get("exit_code", 1)

    # 결과 파싱
    tests_passed = 0
    tests_total = 0
    for line in stdout.splitlines():
        if line.startswith("QA_RESULT:"):
            try:
                parts = line.split(":")[1].strip().split("/")
                tests_passed = int(parts[0])
                tests_total = int(parts[1].split()[0])
            except Exception:
                pass

    tests_failed = tests_total - tests_passed
    qa_status = "pass" if (exit_code == 0 and tests_failed == 0 and tests_total > 0) else "fail"

    qa_result = {
        "status": qa_status,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "tests_total": tests_total,
        "stdout": stdout[:1000],
        "exit_code": exit_code,
        "test_code": test_code,
    }

    existing_qa = list(state.get("qa_test_results", []))
    existing_qa.append(qa_result)

    logger.info("qa_node_done", status=qa_status, passed=tests_passed, total=tests_total)

    return {
        "qa_test_results": existing_qa,
        "checkpoint_stage": "final_review",
        "messages": [AIMessage(
            content=f"QA 완료: {tests_passed}/{tests_total} 테스트 통과 (상태: {qa_status})"
        )],
        **cost_update,
    }
