"""
AADS Docker Sandbox - 로컬 Docker 컨테이너 기반 코드 실행
E2B 대체, 비용 $0. CEO Directive D-011.

보안 제한:
- 메모리: 512MB
- CPU: 1코어
- 네트워크: 비활성화
- 읽기전용 루트
- 타임아웃: 60초
- 최대 동시 컨테이너: 5
"""
import docker
import asyncio
import logging
import tempfile
import os
from typing import Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# 동시 실행 제한
_semaphore = asyncio.Semaphore(5)
_client = None

def _get_docker_client():
    global _client
    if _client is None:
        try:
            _client = docker.from_env()
            _client.ping()
            logger.info("Docker client connected")
        except Exception as e:
            logger.error(f"Docker client failed: {e}")
            _client = None
    return _client

async def execute_code(
    code: str,
    language: str = "python",
    timeout: int = 60
) -> Dict:
    """
    Docker 컨테이너에서 코드를 안전하게 실행.
    Returns: {"stdout": str, "stderr": str, "exit_code": int, "error": bool, "execution_time_ms": int}
    """
    async with _semaphore:
        return await _run_in_container(code, language, timeout)

async def _run_in_container(code: str, language: str, timeout: int) -> Dict:
    start_time = datetime.now()

    image_map = {
        "python": "python:3.12-slim",
        "node": "node:20-slim",
        "javascript": "node:20-slim",
        "typescript": "node:20-slim",
    }
    image = image_map.get(language, "python:3.12-slim")

    cmd_map = {
        "python": ["python", "-c"],
        "node": ["node", "-e"],
        "javascript": ["node", "-e"],
        "typescript": ["node", "-e"],
    }
    cmd_prefix = cmd_map.get(language, ["python", "-c"])

    client = _get_docker_client()
    if not client:
        return _fallback_result(code, "Docker client not available")

    container = None
    try:
        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(None, lambda: client.containers.run(
            image=image,
            command=cmd_prefix + [code],
            detach=True,
            mem_limit="512m",
            cpu_count=1,
            network_disabled=True,
            read_only=True,
            tmpfs={"/tmp": "size=100M"},
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            labels={"aads": "sandbox", "created": datetime.now().isoformat()},
        ))

        result = await loop.run_in_executor(None, lambda: container.wait(timeout=timeout))
        logs = await loop.run_in_executor(None, lambda: container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace"))

        exit_code = result.get("StatusCode", -1)
        elapsed = int((datetime.now() - start_time).total_seconds() * 1000)

        # stdout/stderr 분리
        stdout_logs = await loop.run_in_executor(None, lambda: container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace"))
        stderr_logs = await loop.run_in_executor(None, lambda: container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace"))

        return {
            "stdout": stdout_logs[:10000],
            "stderr": stderr_logs[:5000],
            "exit_code": exit_code,
            "error": exit_code != 0,
            "execution_time_ms": elapsed,
            "language": language,
            "sandbox_type": "docker_local"
        }

    except Exception as e:
        elapsed = int((datetime.now() - start_time).total_seconds() * 1000)
        logger.error(f"Sandbox execution error: {e}")
        return {
            "stdout": "",
            "stderr": str(e)[:5000],
            "exit_code": -1,
            "error": True,
            "execution_time_ms": elapsed,
            "language": language,
            "sandbox_type": "docker_local"
        }
    finally:
        if container:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: container.remove(force=True))
            except Exception:
                pass

async def execute_file(
    file_content: str,
    filename: str,
    language: str = "python",
    timeout: int = 60
) -> Dict:
    """파일 내용을 받아 컨테이너에서 실행"""
    if language == "python":
        # 파일이 길면 -c 대신 파일로 실행
        wrapped_code = file_content
        return await execute_code(wrapped_code, language, timeout)
    else:
        return await execute_code(file_content, language, timeout)

def _fallback_result(code: str, reason: str) -> Dict:
    """Docker 사용 불가 시 코드만 반환 (실행 없음)"""
    logger.warning(f"Sandbox fallback: {reason}")
    return {
        "stdout": f"[FALLBACK] Code not executed. Reason: {reason}\n\n--- Code ---\n{code[:3000]}",
        "stderr": "",
        "exit_code": -1,
        "error": False,
        "execution_time_ms": 0,
        "language": "unknown",
        "sandbox_type": "fallback_code_only"
    }

async def check_sandbox_health() -> Dict:
    """샌드박스 상태 확인"""
    client = _get_docker_client()
    if not client:
        return {"status": "error", "message": "Docker client not available"}

    try:
        loop = asyncio.get_event_loop()
        # 이미지 존재 확인
        images = await loop.run_in_executor(None, lambda: client.images.list())
        image_names = []
        for img in images:
            image_names.extend(img.tags)

        has_python = any("python:3.12-slim" in t for t in image_names)
        has_node = any("node:20-slim" in t for t in image_names)

        # 실행 중인 aads 샌드박스 컨테이너 수
        containers = await loop.run_in_executor(None, lambda: client.containers.list(
            filters={"label": "aads=sandbox"}
        ))

        return {
            "status": "ok",
            "docker_connected": True,
            "python_image": has_python,
            "node_image": has_node,
            "active_sandboxes": len(containers),
            "max_concurrent": 5
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 모듈 로드 시 이미지 풀 시도
async def pull_images():
    client = _get_docker_client()
    if client:
        loop = asyncio.get_event_loop()
        for img in ["python:3.12-slim", "node:20-slim"]:
            try:
                await loop.run_in_executor(None, lambda i=img: client.images.pull(i))
                logger.info(f"Pulled image: {img}")
            except Exception as e:
                logger.warning(f"Failed to pull {img}: {e}")


# Backward-compatible aliases for developer agent
async def execute_in_sandbox(code: str, language: str = "python", timeout: int = 30) -> Dict:
    """Alias for execute_code (used by developer agent)."""
    return await execute_code(code, language, timeout)


async def fallback_code_only(code: str) -> Dict:
    """Async wrapper for _fallback_result (used by developer agent)."""
    return _fallback_result(code, "sandbox_unavailable")
