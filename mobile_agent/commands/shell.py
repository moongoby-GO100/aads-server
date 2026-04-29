"""일반 셸 명령 실행 (보안 필터링)."""
from __future__ import annotations
import subprocess
from typing import Any

from mobile_agent.config import is_command_safe


async def execute(params: dict[str, Any]) -> dict[str, Any]:
    command = params.get("command", "")
    if not command:
        return {"status": "error", "data": {"error": "command 필수"}}
    if not is_command_safe(command):
        return {"status": "error", "data": {"error": f"차단된 명령: {command}"}}
    timeout = min(params.get("timeout", 30), 60)
    try:
        result = subprocess.run(
            command, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "status": "success" if result.returncode == 0 else "error",
            "data": {
                "stdout": result.stdout[:10000],
                "stderr": result.stderr[:2000],
                "returncode": result.returncode,
            },
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "data": {"error": f"명령 {timeout}초 초과"}}
