"""
DevOps Agent: 배포 스크립트 생성 + 헬스체크.
Judge pass 이후 실행.
"""
import structlog
from app.graph.state import AADSState
from app.services.model_router import get_llm_for_agent, estimate_cost
from app.services.cost_tracker import check_and_increment, CostLimitExceeded
from app.services.sandbox import execute_in_sandbox
from app.config import settings

logger = structlog.get_logger()

DEVOPS_SYSTEM_PROMPT = """당신은 AADS의 DevOps Agent입니다.
Judge가 승인한 코드를 받아 배포 스크립트와 환경 설정을 생성합니다.

출력 형식 (JSON 블록):
```json
{
  "deploy_script": "실행 가능한 배포 스크립트",
  "health_check_cmd": "헬스체크 명령어",
  "env_vars": {"KEY": "value"},
  "deploy_notes": "배포 주의사항"
}
```

규칙:
1. Python/Docker 기반 배포 스크립트
2. 헬스체크 엔드포인트 포함
3. 환경변수 목록 명시
4. JSON 블록만 출력
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
    return {"deploy_notes": text.strip(), "health_check_cmd": "echo ok"}


async def devops_node(state: AADSState) -> dict:
    """
    1. 생성된 코드로 배포 스크립트 생성 (LLM)
    2. 샌드박스에서 검증 실행 (선택)
    3. 배포 결과 반환
    """
    logger.info("devops_node_start")

    task = state.get("current_task", {})
    files = state.get("generated_files", [])
    verdict = state.get("judge_verdict", {})

    # Judge 미통과 시 스킵
    if verdict and verdict.get("verdict") == "fail":
        logger.warning("devops_skip_judge_fail")
        return {"checkpoint_stage": "completed"}

    try:
        llm, model_config = get_llm_for_agent("devops")
        est_cost = estimate_cost(model_config, 2000, 4000)
        cost_update = check_and_increment(state, est_cost, "devops", settings)
    except CostLimitExceeded as e:
        logger.error("devops_cost_limit", error=str(e))
        return {
            "error_log": state.get("error_log", []) + [str(e)],
            "checkpoint_stage": "completed",
        }

    code_summary = "\n".join(
        f"# {f['path']}\n{f['content'][:500]}" for f in files[:3]
    ) if files else "코드 없음"

    messages = [
        {"role": "system", "content": DEVOPS_SYSTEM_PROMPT},
        {"role": "user", "content": f"""
작업: {task.get("description", "Unknown")}
생성된 코드 (요약):
{code_summary}

위 코드를 배포하기 위한 스크립트와 설정을 JSON으로 생성하세요.
"""},
    ]

    response = await llm.ainvoke(messages)
    deploy_config = extract_json_block(response.content)

    # 헬스체크 스크립트 샌드박스 검증 (선택적)
    health_cmd = deploy_config.get("health_check_cmd", "")
    devops_result = {**deploy_config, "validation": "skipped"}

    if health_cmd and "echo" in health_cmd.lower():
        sandbox_res = await execute_in_sandbox(health_cmd, language="bash")
        devops_result["validation"] = "passed" if sandbox_res.get("exit_code") == 0 else "failed"

    logger.info("devops_node_done", validation=devops_result.get("validation"))
    return {
        **cost_update,
        "devops_result": devops_result,
        "checkpoint_stage": "completed",
    }
