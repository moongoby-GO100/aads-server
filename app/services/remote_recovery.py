"""
AADS-133: 다중 경로 원격 복구 모듈
direct → relay_X → relay_Y 순서로 시도, 성공 시 즉시 반환
watchdog_daemon.py와 escalation_engine.py에서 import하여 사용
"""
import asyncio
import subprocess
import time
from typing import Optional
import structlog

logger = structlog.get_logger()

# ─── 복구 경로 정의 ────────────────────────────────────────────────────────────

RECOVERY_ROUTES: dict[str, list[dict]] = {
    "114": [
        {"via": "direct",     "method": "ssh",       "host": "서버114_IP"},
        {"via": "relay_211",  "method": "ssh_relay",  "relay": "211.188.51.113", "target": "서버114_IP"},
        {"via": "relay_68",   "method": "ssh_relay",  "relay": "서버68_IP",      "target": "서버114_IP"},
    ],
    "211": [
        {"via": "direct",     "method": "ssh",       "host": "211.188.51.113"},
        {"via": "relay_68",   "method": "ssh_relay",  "relay": "서버68_IP",      "target": "211.188.51.113"},
        {"via": "relay_114",  "method": "ssh_relay",  "relay": "서버114_IP",     "target": "211.188.51.113"},
    ],
    "68": [
        {"via": "direct",     "method": "ssh",       "host": "서버68_IP"},
        {"via": "relay_211",  "method": "ssh_relay",  "relay": "211.188.51.113", "target": "서버68_IP"},
        {"via": "relay_114",  "method": "ssh_relay",  "relay": "서버114_IP",     "target": "서버68_IP"},
    ],
}

# 실행 이력 (메모리, 재시작 시 초기화)
recovery_logs: list[dict] = []

SSH_TIMEOUT = 10  # seconds


def _run_ssh_direct(host: str, command: str, timeout: int = SSH_TIMEOUT) -> tuple[bool, str]:
    """SSH 직접 연결로 명령 실행"""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no",
             f"root@{host}", command],
            capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip() or result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def _run_ssh_relay(relay: str, target: str, command: str, timeout: int = SSH_TIMEOUT) -> tuple[bool, str]:
    """SSH 릴레이 경유 명령 실행 (ProxyJump)"""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no",
             "-J", f"root@{relay}",
             f"root@{target}", command],
            capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip() or result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


async def remote_execute(server: str, command: str) -> tuple[bool, str]:
    """
    routes에 정의된 순서대로 시도.
    각 시도 결과를 recovery_logs에 기록 (route 경로 포함).
    성공 시 즉시 반환, 전체 실패 시 False 반환.
    """
    routes = RECOVERY_ROUTES.get(server, [])
    if not routes:
        logger.error("remote_execute: unknown server", server=server)
        return False, f"unknown server: {server}"

    attempt_time = time.strftime("%Y-%m-%dT%H:%M:%S")

    for route in routes:
        via = route["via"]
        method = route["method"]

        logger.info("remote_execute attempt", server=server, via=via, command=command)

        if method == "ssh":
            success, output = await asyncio.get_event_loop().run_in_executor(
                None, _run_ssh_direct, route["host"], command
            )
        elif method == "ssh_relay":
            success, output = await asyncio.get_event_loop().run_in_executor(
                None, _run_ssh_relay, route["relay"], route["target"], command
            )
        else:
            success, output = False, f"unknown method: {method}"

        log_entry = {
            "time": attempt_time,
            "server": server,
            "via": via,
            "command": command,
            "success": success,
            "output": output[:500],  # 최대 500자
        }
        recovery_logs.append(log_entry)

        if success:
            logger.info("remote_execute success", server=server, via=via, output=output[:200])
            return True, output
        else:
            logger.warning("remote_execute failed", server=server, via=via, output=output[:200])

    logger.error("remote_execute: all routes failed", server=server, command=command)
    return False, "all routes failed"


async def remote_kill_task(server: str, task_id: str) -> bool:
    """원격 서버에서 task_id를 포함하는 프로세스 종료"""
    command = f"pgrep -f '{task_id}' | xargs kill -TERM"
    success, _ = await remote_execute(server, command)
    return success


async def remote_restart_service(server: str, service_name: str) -> bool:
    """원격 서버에서 systemd 서비스 재시작"""
    command = f"systemctl restart {service_name}"
    success, _ = await remote_execute(server, command)
    return success


def get_recovery_logs(limit: int = 50) -> list[dict]:
    """최근 복구 로그 반환"""
    return recovery_logs[-limit:]
