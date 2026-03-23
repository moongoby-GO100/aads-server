"""AADS-195: 셸 명령 실행."""
from __future__ import annotations

import logging
import subprocess
from typing import Any, Dict

logger = logging.getLogger(__name__)

# 보안: 차단 명령 패턴
_BLOCKED_COMMANDS = [
    "format", "del /s", "rd /s", "rmdir /s",
    "shutdown", "rm -rf", "mkfs",
]


async def execute(params: Dict[str, Any]) -> Dict[str, Any]:
    """셸 명령 실행 — 타임아웃 30초, 위험 명령 차단."""
    command = params.get("command", "")
    if not command:
        return {"status": "error", "data": {"error": "명령어가 비어있습니다."}}

    # 위험 명령 차단
    cmd_lower = command.lower().strip()
    for blocked in _BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            return {"status": "error", "data": {"error": f"차단된 명령: {blocked}"}}

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "status": "success",
            "data": {
                "output": result.stdout[-4000:] if result.stdout else "",
                "error_output": result.stderr[-2000:] if result.stderr else "",
                "exit_code": result.returncode,
            },
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "data": {"error": "명령 실행 타임아웃 (30초)"}}
    except Exception as e:
        logger.error("shell_execute_error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
