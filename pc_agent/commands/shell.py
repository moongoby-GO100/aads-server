"""AADS-195: 셸 명령 실행."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from typing import Any, Dict

logger = logging.getLogger(__name__)

# 보안: 차단 명령 패턴
_BLOCKED_COMMANDS = [
    "format", "del /s", "rd /s", "rmdir /s",
    "shutdown", "rm -rf", "mkfs",
]


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Best-effort termination including descendants spawned by shell=True."""
    if os.name == "nt":
        taskkill_kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            taskkill_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                **taskkill_kwargs,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _run_shell_command(command: str, kwargs: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Run one command with a hard deadline and descendant cleanup."""
    popen_kwargs: dict[str, Any] = dict(kwargs)
    if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = (
            int(popen_kwargs.get("creationflags", 0))
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    elif os.name != "nt":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


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
        # Windows: CREATE_NO_WINDOW로 cmd.exe 콘솔 깜박임 방지
        kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        # subprocess.run() is blocking. Running it directly in this async
        # handler stalls the PC Agent WebSocket loop, including heartbeats and
        # reconnect handling. A long command previously caused the server
        # command timeout and WebSocket code=1005 disconnect in the same
        # 30-second window. Keep the subprocess deadline, but isolate the wait
        # from the networking event loop.
        result = await asyncio.to_thread(_run_shell_command, command, kwargs)
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
