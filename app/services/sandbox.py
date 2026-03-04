"""
AADS 샌드박스 — 2단계 하이브리드 (D-011, CEO 확정 2026-03-03)
1순위: Docker 로컬 (소형, 기본)
2순위: SSH 원격 (대형, Phase 3)
3순위: E2B SaaS (Phase 3+ 옵션)
4순위: fallback_code_only (모두 실패 시)
"""
import asyncio
import structlog
from typing import Optional

logger = structlog.get_logger()


class DockerSandbox:
    """자체 서버 Docker 컨테이너 샌드박스"""

    def __init__(self, image: str = "python:3.12-slim", timeout: int = 300):
        self.image = image
        self.timeout = timeout

    async def execute(self, code: str, language: str = "python") -> dict:
        """Docker 컨테이너에서 코드 격리 실행"""
        try:
            import docker
            client = docker.from_env()
        except Exception as e:
            logger.warning("docker_sdk_unavailable", error=str(e))
            return await fallback_code_only(code)

        try:
            if language == "python":
                cmd = ["python3", "-c", code]
                img = "python:3.12-slim"
            elif language in ("javascript", "node"):
                cmd = ["node", "-e", code]
                img = "node:20-slim"
            else:
                cmd = ["python3", "-c", code]
                img = "python:3.12-slim"

            container = client.containers.run(
                image=img,
                command=cmd,
                detach=True,
                network_mode="none",
                mem_limit="512m",
                nano_cpus=1_000_000_000,  # 1 CPU
                read_only=True,
                tmpfs={"/tmp": "size=100M"},
            )

            result = container.wait(timeout=self.timeout)
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            exit_code = result.get("StatusCode", 1)

            container.remove(force=True)

            logger.info("docker_sandbox_success", exit_code=exit_code, stdout_len=len(stdout))
            return {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "error": exit_code != 0,
                "sandbox_type": "docker_local",
                "sandbox_id": container.short_id if hasattr(container, 'short_id') else None,
            }
        except Exception as e:
            logger.error("docker_sandbox_failed", error=str(e))
            return await fallback_code_only(code)


class E2BSandbox:
    """E2B 샌드박스 — Phase 3+ SaaS 확장 옵션 (현재 사용 안 함)"""

    async def execute(self, code: str, language: str = "python", timeout: int = 60) -> dict:
        from e2b_code_interpreter import AsyncSandbox
        from app.config import settings

        sandbox = None
        try:
            sandbox = await AsyncSandbox.create(
                api_key=settings.E2B_API_KEY.get_secret_value(),
                timeout=settings.SANDBOX_TIMEOUT_SECONDS,
            )
            execution = await sandbox.run_code(code)
            return {
                "stdout": execution.text or "",
                "stderr": getattr(execution, "error", "") or "",
                "exit_code": 0 if not execution.error else 1,
                "error": False,
                "sandbox_type": "e2b",
                "sandbox_id": sandbox.sandbox_id,
            }
        except Exception as e:
            logger.error("e2b_sandbox_failed", error=str(e))
            return await fallback_code_only(code)
        finally:
            if sandbox:
                try:
                    await sandbox.kill()
                except Exception:
                    pass


async def execute_in_sandbox(code: str, language: str = "python", timeout: int = 60) -> dict:
    """메인 샌드박스 실행 함수 — Docker 로컬 우선"""
    sandbox = DockerSandbox(timeout=timeout)
    return await sandbox.execute(code, language)


async def fallback_code_only(code: str) -> dict:
    """모든 샌드박스 실패 시 Graceful Degradation"""
    logger.warning("sandbox_fallback_code_only")
    return {
        "stdout": "[Sandbox unavailable] Code generated but not executed.",
        "stderr": "",
        "exit_code": 0,
        "error": False,
        "sandbox_type": "fallback",
        "sandbox_id": None,
        "code": code,
    }
