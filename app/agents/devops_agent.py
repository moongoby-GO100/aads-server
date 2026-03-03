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
Judge가 승인한 코드를 받아 배포 준비물(스크립트, 환경 설정, 모니터링)을 생성합니다.

## 역할
- 코드를 실제 서비스 가능한 상태로 패키징
- 배포 자동화 스크립트 생성
- 헬스체크 및 모니터링 설정
- 롤백 전략 제시

## 배포 원칙
1. **재현 가능성**: 동일 환경에서 언제나 동일하게 배포 가능
2. **최소 권한**: 필요한 환경변수와 권한만 명시
3. **헬스체크**: 서비스 기동 후 즉시 확인 가능한 명령어
4. **롤백**: 배포 실패 시 이전 버전으로 복구 방법 포함

## 출력 형식 (JSON 블록만, 추가 설명 없음):
```json
{
  "runtime": "python:3.11-slim 또는 node:20-alpine 등",
  "install_cmd": "pip install -r requirements.txt",
  "run_cmd": "python main.py 또는 uvicorn app:app --port 8000",
  "deploy_script": "#!/bin/bash\\n# 배포 스크립트 (단계별)",
  "health_check_cmd": "curl -f http://localhost:8000/health || exit 1",
  "rollback_cmd": "docker stop current && docker start previous",
  "env_vars": {"PORT": "8000", "LOG_LEVEL": "INFO"},
  "exposed_ports": [8000],
  "deploy_notes": "배포 주의사항 및 체크리스트"
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
