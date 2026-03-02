"""
E2B 샌드박스 래퍼.
⚠️ e2b-code-interpreter 2.4.1 사용.
⚠️ 비동기: AsyncSandbox.create() / sandbox.run_code()
"""
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

logger = structlog.get_logger()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def execute_in_sandbox(
    code: str,
    language: str = "python",
    timeout: int = 60,
) -> dict:
    """
    E2B 샌드박스에서 코드 실행.
    Returns: {stdout, stderr, exit_code, error, sandbox_id}
    """
    from e2b_code_interpreter import AsyncSandbox
    from app.config import settings

    sandbox = None
    try:
        sandbox = await AsyncSandbox.create(
            api_key=settings.E2B_API_KEY.get_secret_value(),
            timeout=settings.SANDBOX_TIMEOUT_SECONDS,
        )

        execution = await sandbox.run_code(code)

        result = {
            "stdout": execution.text or "",
            "stderr": getattr(execution, "error", "") or "",
            "exit_code": 0 if not execution.error else 1,
            "error": False,
            "sandbox_id": sandbox.sandbox_id,
        }
        logger.info("sandbox_execution_success", sandbox_id=sandbox.sandbox_id)
        return result

    except Exception as e:
        logger.error("sandbox_execution_failed", error=str(e))
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": 1,
            "error": True,
            "sandbox_id": getattr(sandbox, "sandbox_id", None) if sandbox else None,
        }
    finally:
        if sandbox:
            try:
                await sandbox.kill()
            except Exception:
                pass


async def fallback_code_only(code: str) -> dict:
    """
    E2B 전면 실패 시 Graceful Degradation.
    코드는 반환하되 실행은 하지 않음.
    """
    logger.warning("sandbox_fallback_code_only")
    return {
        "stdout": "[E2B unavailable] Code generated but not executed.",
        "stderr": "",
        "exit_code": 0,  # graceful degradation
        "error": False,
        "sandbox_id": None,
        "code": code,
    }
