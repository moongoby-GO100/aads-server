"""AADS-195: 프로세스 관리."""
from __future__ import annotations

import logging
import subprocess
from typing import Any, Dict

import psutil

logger = logging.getLogger(__name__)


async def execute(params: Dict[str, Any]) -> Dict[str, Any]:
    """실행 중인 프로세스 목록 반환."""
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        processes = []
        for line in result.stdout.strip().split("\n"):
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 5:
                processes.append({
                    "name": parts[0],
                    "pid": parts[1],
                    "memory": parts[4],
                })
        return {"status": "success", "data": {"processes": processes[:100]}}
    except FileNotFoundError:
        # Linux 환경 폴백
        try:
            result = subprocess.run(
                ["ps", "aux", "--sort=-rss"],
                capture_output=True, text=True, timeout=10,
            )
            lines = result.stdout.strip().split("\n")[:101]
            return {"status": "success", "data": {"processes": lines}}
        except Exception:
            return {"status": "error", "data": {"error": "프로세스 목록 조회 실패"}}
    except Exception as e:
        logger.error("process_list_error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def process_kill(params: Dict[str, Any]) -> Dict[str, Any]:
    """PID로 프로세스 종료."""
    pid = params.get("pid")
    if pid is None:
        return {"status": "error", "data": {"error": "pid 파라미터 필수"}}

    try:
        pid = int(pid)
        proc = psutil.Process(pid)
        proc_name = proc.name()
        proc.kill()
        proc.wait(timeout=5)
        logger.info("process_killed pid=%d name=%s", pid, proc_name)
        return {"status": "success", "data": {"pid": pid, "name": proc_name}}
    except psutil.NoSuchProcess:
        return {"status": "error", "data": {"error": f"프로세스 없음: pid={pid}"}}
    except psutil.AccessDenied:
        return {"status": "error", "data": {"error": f"권한 없음: pid={pid}"}}
    except Exception as e:
        logger.error("process_kill_error pid=%s: %s", pid, e)
        return {"status": "error", "data": {"error": str(e)}}
