"""
AADS Unified Self-Healing Engine
- 30초 주기 서비스 헬스체크 + 자동복구 + CEO 승인 연동
- 끊어진 6대 파이프라인을 단일 엔진으로 통합

흐름:
  monitored_services 순회 → 상태 체크 → consecutive_failures 업데이트
  → 임계값 초과 시 auto_recovery_command 실행 (안전) 또는 approval_queue 등록 (위험)
  → error_log 미해결 건 스캔 → 매칭 복구 명령 실행/승인 요청
  → 성공 시 error_log resolved 처리, alert 자동 acknowledge
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

import structlog

logger = structlog.get_logger("unified_healer")

# ── 안전/위험 명령 분류 ──────────────────────────────────────────────────────

SAFE_COMMANDS = {
    "docker restart aads-server",
    "docker restart aads-postgres",
    "docker restart aads-redis",
    "docker restart aads-litellm",
    "systemctl restart nginx",
    "systemctl reload nginx",
    "docker stop -t 30 aads-server",
    "docker system prune -f",
    "/root/aads/aads-server/deploy.sh bluegreen",
}

SAFE_PREFIXES = [
    "docker restart ",
    "docker stop -t ",
    "docker compose restart ",
    "systemctl restart ",
    "systemctl reload ",
    "supervisorctl reload ",
]

RISKY_PREFIXES = [
    "reboot",
    "rm ",
    "docker compose down",
    "docker stop",
    "kill -9",
    "DROP ",
]

# ── 에러 타입 → 복구 명령 매핑 ───────────────────────────────────────────────

ERROR_RECOVERY_MAP = {
    "service_down_docker": "docker restart {service}",
    "service_down_http_health": "docker restart {service}",
    "container_exit": "docker restart {service}",
    "docker_crash": "docker restart {service}",
    "api_unreachable": "/root/aads/aads-server/deploy.sh bluegreen",
    "nginx_down": "systemctl restart nginx",
    "nginx_error": "systemctl reload nginx",
    "disk_space_critical": "docker system prune -f",
    "high_memory": "/root/aads/aads-server/deploy.sh bluegreen",
    "db_connection_error": "docker restart aads-postgres",
    "redis_connection_error": "docker restart aads-redis",
}

# ── 서킷브레이커: 동일 서비스 연속 복구 실패 추적 ─────────────────────────────

_circuit_breaker: dict[str, dict] = {}
CIRCUIT_BREAKER_MAX_FAILURES = 1  # 1회 실패로 즉시 차단 (무한 루프 방지)
CIRCUIT_BREAKER_COOLDOWN_SEC = 1800  # 30분 쿨다운 (재시도 방지)

# ── DB 헬퍼 ──────────────────────────────────────────────────────────────────

def _get_db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql://", "postgres://") if url else url


async def _get_conn():
    import asyncpg
    return await asyncpg.connect(_get_db_url(), timeout=10)


# ── Docker API 헬퍼 ──────────────────────────────────────────────────────────

def _docker_curl_args(endpoint: str, method: str = "GET", timeout_sec: int = 10) -> list[str]:
    """DOCKER_HOST 환경변수에 따라 curl 인자 생성 (tcp proxy 또는 unix socket)."""
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host.startswith("tcp://"):
        base_url = docker_host.replace("tcp://", "http://")
        url = f"{base_url}/v1.24{endpoint}"
        args = ["curl", "-sf", url, "--max-time", str(timeout_sec)]
    else:
        url = f"http://localhost/v1.24{endpoint}"
        args = ["curl", "-sf", "--unix-socket", "/var/run/docker.sock", url, "--max-time", str(timeout_sec)]
    if method != "GET":
        args.insert(2, "-X")
        args.insert(3, method)
    return args


# ── 서비스 체크 ──────────────────────────────────────────────────────────────

async def _check_service(svc: dict) -> str:
    """서비스 상태 체크. 'ok' 또는 'fail' 반환."""
    check_type = svc.get("check_type", "http")
    target = svc.get("check_target", "")
    timeout_sec = svc.get("timeout", 10)

    try:
        if check_type == "docker":
            # Docker Engine API via proxy 또는 unix socket
            args = _docker_curl_args(f"/containers/{target}/json", timeout_sec=timeout_sec)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec + 2)
            if b'"Running":true' in stdout:
                return "ok"
            # Created/Exited 상태 = 컨테이너 존재하지만 미시작 → docker start로 복구
            if b'"Status":"created"' in stdout or b'"Status":"exited"' in stdout:
                logger.warning("container_not_running", container=target,
                               status="created/exited", action="auto_start")
                await _docker_api("start", target)
                return "fail"  # 다음 사이클에서 재확인
            return "fail"

        elif check_type in ("http", "https", "http_health"):
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
                "--max-time", str(timeout_sec), target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec + 5)
            code = stdout.decode().strip()
            return "ok" if code.startswith(("2", "3")) else "fail"

        elif check_type == "tcp":
            # target = "host:port"
            parts = target.rsplit(":", 1)
            if len(parts) == 2:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(parts[0], int(parts[1])),
                    timeout=timeout_sec,
                )
                writer.close()
                await writer.wait_closed()
                return "ok"
            return "fail"

        elif check_type == "process":
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-f", target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            return "ok" if proc.returncode == 0 else "fail"

        elif check_type == "ssh_command":
            # SSH를 통한 원격 서버 명령 실행 (systemctl is-active 등)
            result = await _execute_command(target, svc.get("server", "68"))
            return "ok" if result.get("success") else "fail"

        else:
            return "unknown"

    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("service_check_error", service=svc.get("service_name"), error=str(e))
        return "fail"


# ── 명령 안전성 판별 ─────────────────────────────────────────────────────────

def _is_safe_command(cmd: str) -> bool:
    """화이트리스트 기반 안전 명령 판별."""
    cmd = cmd.strip()
    if cmd in SAFE_COMMANDS:
        return True
    if any(cmd.startswith(p) for p in SAFE_PREFIXES):
        return True
    return False


def _is_risky_command(cmd: str) -> bool:
    """위험 명령 판별."""
    cmd = cmd.strip()
    return any(cmd.startswith(p) for p in RISKY_PREFIXES)


# ── 서킷브레이커 ─────────────────────────────────────────────────────────────

def _circuit_open(service_key: str) -> bool:
    """서킷 오픈(차단) 상태인지 확인."""
    cb = _circuit_breaker.get(service_key)
    if not cb:
        return False
    if cb["failures"] >= CIRCUIT_BREAKER_MAX_FAILURES:
        if time.time() - cb["last_failure"] < CIRCUIT_BREAKER_COOLDOWN_SEC:
            return True
        # 쿨다운 지남 → 리셋
        _circuit_breaker.pop(service_key, None)
        return False
    return False


def _circuit_record_failure(service_key: str):
    cb = _circuit_breaker.setdefault(service_key, {"failures": 0, "last_failure": 0})
    cb["failures"] += 1
    cb["last_failure"] = time.time()


def _circuit_record_success(service_key: str):
    _circuit_breaker.pop(service_key, None)


# ── 명령 실행 ────────────────────────────────────────────────────────────────

async def _docker_api(action: str, container: str) -> dict:
    """Docker Engine API via Unix socket (컨테이너 내부에서 docker CLI 없이 실행)."""
    import re
    endpoint = ""
    if action == "restart":
        endpoint = f"/containers/{container}/restart"
    elif action == "stop":
        endpoint = f"/containers/{container}/stop"
    elif action == "start":
        endpoint = f"/containers/{container}/start"
    elif action == "inspect":
        endpoint = f"/containers/{container}/json"
    else:
        return {"success": False, "output": f"Unknown docker action: {action}"}

    try:
        args = _docker_curl_args(endpoint, method="POST", timeout_sec=60)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=65)
        # restart/stop/start return 204 No Content on success
        success = proc.returncode == 0
        output = (stdout.decode()[:300] + stderr.decode()[:200]).strip()
        return {"success": success, "output": output or f"{action} {container}: ok"}
    except Exception as e:
        return {"success": False, "output": str(e)[:300]}


def _parse_docker_command(command: str) -> tuple[str, str]:
    """'docker restart aads-server' → ('restart', 'aads-server')."""
    parts = command.strip().split()
    if len(parts) >= 3 and parts[0] == "docker":
        return parts[1], parts[2]
    return "", ""


async def _execute_command(command: str, target_server: str = "68") -> dict:
    """명령 실행. 68서버 docker 명령은 Unix Socket API로, 그 외=SSH."""
    try:
        if target_server == "68":
            # docker 명령은 Docker Engine API로 직접 실행
            if command.strip().startswith("docker "):
                action, container = _parse_docker_command(command)
                # 동일 compose 프로젝트 컨테이너 restart 차단 — 내부에서 재시작하면 DB 끊김/무한루프
                BLOCKED_CONTAINERS = {"aads-server", "aads-postgres", "aads-redis", "aads-litellm"}
                if container in BLOCKED_CONTAINERS and action in ("restart", "stop"):
                    logger.warning("sibling_restart_blocked", command=command, container=container,
                                   reason="Cannot restart sibling containers from inside — causes DB connection loss")
                    return {"success": True, "output": f"Restart blocked for {container} (use external watchdog)"}
                if action and container:
                    return await _docker_api(action, container)
                return {"success": False, "output": f"Cannot parse docker command: {command}"}

            # systemctl, supervisorctl 등은 호스트에서 실행 불가 → SSH로 fallback
            # 68서버 자신에게 SSH (localhost)
            ssh_cmd = f'ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@localhost "{command}"'
            proc = await asyncio.create_subprocess_shell(
                ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            return {
                "success": proc.returncode == 0,
                "output": (stdout.decode()[:500] + stderr.decode()[:500]).strip(),
            }
        else:
            ssh_key_map = {
                "211": "/root/.ssh/id_ed25519_newtalk",
                "114": "/root/.ssh/id_ed25519_newtalk",
            }
            ssh_host_map = {
                "211": os.getenv("SERVER_211_HOST", "211.188.51.113"),
                "114": os.getenv("SERVER_114_HOST", ""),
            }
            key = ssh_key_map.get(target_server, "")
            host = ssh_host_map.get(target_server, "")
            if not host:
                return {"success": False, "output": f"No SSH config for {target_server}"}

            ssh_cmd = (
                f'ssh -i {key} -o StrictHostKeyChecking=no -o ConnectTimeout=10 '
                f'root@{host} "{command}"'
            )
            proc = await asyncio.create_subprocess_shell(
                ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            return {
                "success": proc.returncode == 0,
                "output": (stdout.decode()[:500] + stderr.decode()[:500]).strip(),
            }
    except asyncio.TimeoutError:
        return {"success": False, "output": "Command timed out (120s)"}
    except Exception as e:
        return {"success": False, "output": str(e)[:500]}


# ── 텔레그램 알림 ────────────────────────────────────────────────────────────

async def _notify_telegram(message: str):
    """간단한 텔레그램 알림 (봇 모듈 임포트)."""
    try:
        from app.services.telegram_bot import get_telegram_bot
        bot = get_telegram_bot()
        if bot and bot.is_ready:
            await bot.send_message(message)
    except Exception as e:
        logger.debug("telegram_notify_failed", error=str(e))


# ── 승인 요청 생성 ───────────────────────────────────────────────────────────

async def _create_approval_request(
    conn, title: str, description: str, command: str,
    target_server: str, severity: str, error_log_id: int = None,
):
    """approval_queue에 INSERT + 텔레그램 인라인 버튼 발송."""
    import urllib.request
    import urllib.error

    row = await conn.fetchrow("""
        INSERT INTO approval_queue
            (error_log_id, title, description, suggested_action,
             action_type, action_command, target_server, severity)
        VALUES ($1, $2, $3, $4, 'auto_command', $5, $6, $7)
        RETURNING id
    """, error_log_id, title, description, command, command, target_server, severity)

    approval_id = row["id"]

    # 텔레그램 인라인 버튼 발송
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if bot_token and chat_id:
        severity_emoji = {"critical": "\U0001f534", "high": "\U0001f7e0", "medium": "\U0001f7e1", "low": "\U0001f7e2"}
        emoji = severity_emoji.get(severity, "\u26aa")
        text = (
            f"{emoji} *AADS Healer \uc2b9\uc778 \uc694\uccad #{approval_id}*\n\n"
            f"*\uc11c\ubc84*: {target_server}\n"
            f"*\uc81c\ubaa9*: {title}\n"
            f"*\uc124\uba85*: {description[:300]}\n\n"
            f"*\uba85\ub839*: `{command[:200]}`\n"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "\u2705 \uc2b9\uc778", "callback_data": f"approve_{approval_id}"},
                {"text": "\u274c \ubc18\ub824", "callback_data": f"reject_{approval_id}"},
            ]]
        }
        payload = json.dumps({
            "chat_id": chat_id, "text": text,
            "parse_mode": "Markdown", "reply_markup": keyboard,
        }).encode()
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.debug("approval_telegram_failed", error=str(e))

    logger.info("approval_request_created", id=approval_id, title=title)
    return approval_id


# ══════════════════════════════════════════════════════════════════════════════
#  메인 힐링 사이클 (30초 주기)
# ══════════════════════════════════════════════════════════════════════════════

async def healing_cycle():
    """통합 자율복구 사이클. APScheduler에서 30초마다 호출."""
    try:
        conn = await _get_conn()
    except Exception as e:
        logger.warning("healer_db_connect_failed", error=str(e))
        return

    try:
        await _phase1_service_check(conn)
        await _phase2_error_scan(conn)
        await _phase3_alert_auto_acknowledge(conn)
    except Exception as e:
        logger.error("healing_cycle_error", error=str(e))
    finally:
        await conn.close()


# ── Phase 1: 서비스 헬스체크 ─────────────────────────────────────────────────

async def _phase1_service_check(conn):
    """monitored_services 순회 → 상태 체크 → 실패 시 자동복구."""
    rows = await conn.fetch("""
        SELECT id, server, service_name, check_type, check_target,
               timeout, auto_recovery_command, consecutive_failures, enabled
        FROM monitored_services
        WHERE enabled = true
    """)

    for svc in rows:
        svc_dict = dict(svc)
        service_key = f"{svc['server']}:{svc['service_name']}"
        status = await _check_service(svc_dict)

        if status == "ok":
            # 성공: 연속실패 카운트 리셋
            if svc["consecutive_failures"] > 0:
                await conn.execute("""
                    UPDATE monitored_services
                    SET last_status='ok', consecutive_failures=0, last_check=NOW()
                    WHERE id=$1
                """, svc["id"])
                _circuit_record_success(service_key)
                logger.info("service_recovered", service=service_key)
            else:
                await conn.execute("""
                    UPDATE monitored_services
                    SET last_status='ok', last_check=NOW()
                    WHERE id=$1
                """, svc["id"])
        else:
            # 실패: 연속실패 카운트 증가
            new_failures = svc["consecutive_failures"] + 1
            await conn.execute("""
                UPDATE monitored_services
                SET last_status='fail', consecutive_failures=$2, last_check=NOW()
                WHERE id=$1
            """, svc["id"], new_failures)

            logger.warning(
                "service_check_failed",
                service=service_key,
                consecutive=new_failures,
            )

            # 5회 연속 실패 시 자동복구 시도 (3→5: CEO 채팅 끊김 방지)
            if new_failures >= 5 and svc["auto_recovery_command"]:
                await _try_service_recovery(
                    conn, svc, service_key, new_failures
                )


async def _try_service_recovery(conn, svc, service_key: str, failures: int):
    """서비스 복구 시도. 서킷브레이커 + 스트리밍 가드 + 안전성 검사."""
    command = svc["auto_recovery_command"]

    # ── ABSOLUTE BLOCK: aads-api/aads-server 자기 자신 재시작 절대 금지 ──
    # healer는 aads-api 내부에서 실행됨 → 자기 프로세스 kill = 무한 재시작 루프
    # CEO 채팅 끊김의 근본 원인 (2026-04-01 확인: 2시간 내 6회 자살)
    if command and ("aads-api" in command or "aads-server" in command):
        logger.warning(
            "self_restart_blocked",
            service=service_key,
            command=command,
            reason="Healer runs inside aads-api — self-restart causes infinite kill loop and CEO chat disconnection",
        )
        return  # 절대 실행하지 않음 — 외부 watchdog만 허용

    # 서킷브레이커 체크
    if _circuit_open(service_key):
        logger.warning("circuit_breaker_open", service=service_key)
        return

    if _is_safe_command(command):
        # 안전 명령: 즉시 실행
        logger.info("auto_recovery_start", service=service_key, command=command)
        result = await _execute_command(command, svc["server"])

        if result["success"]:
            _circuit_record_success(service_key)
            await conn.execute("""
                UPDATE monitored_services
                SET consecutive_failures=0, last_status='recovering'
                WHERE id=$1
            """, svc["id"])
            await _notify_telegram(
                f"\u2705 *\uc790\ub3d9\ubcf5\uad6c \uc131\uacf5*\n"
                f"\uc11c\ube44\uc2a4: {service_key}\n"
                f"\uba85\ub839: `{command}`\n"
                f"\uacb0\uacfc: {result['output'][:200]}"
            )
            logger.info("auto_recovery_success", service=service_key)
        else:
            _circuit_record_failure(service_key)
            await _notify_telegram(
                f"\u26a0\ufe0f *\uc790\ub3d9\ubcf5\uad6c \uc2e4\ud328*\n"
                f"\uc11c\ube44\uc2a4: {service_key}\n"
                f"\uba85\ub839: `{command}`\n"
                f"\uc5d0\ub7ec: {result['output'][:200]}"
            )
            logger.warning("auto_recovery_failed", service=service_key, output=result["output"][:200])

            # 서킷브레이커 열림 → CEO 승인 요청
            if _circuit_open(service_key):
                await _create_approval_request(
                    conn,
                    title=f"{service_key} \ubcf5\uad6c \uc2e4\ud328 ({CIRCUIT_BREAKER_MAX_FAILURES}\ud68c)",
                    description=f"\uc790\ub3d9\ubcf5\uad6c {CIRCUIT_BREAKER_MAX_FAILURES}\ud68c \uc5f0\uc18d \uc2e4\ud328. \uc218\ub3d9 \ud655\uc778 \ud544\uc694.\n\ub9c8\uc9c0\ub9c9 \uc5d0\ub7ec: {result['output'][:200]}",
                    command=command,
                    target_server=svc["server"],
                    severity="high",
                )

    elif not _is_risky_command(command):
        # 미분류 명령: CEO 승인 요청
        await _create_approval_request(
            conn,
            title=f"{service_key} \ubcf5\uad6c \uc2b9\uc778 \ud544\uc694",
            description=f"\uc5f0\uc18d {failures}\ud68c \uc2e4\ud328. \ubcf5\uad6c \uba85\ub839 \uc2b9\uc778 \ud544\uc694.",
            command=command,
            target_server=svc["server"],
            severity="high" if failures >= 5 else "medium",
        )
    else:
        # 위험 명령: 경고만
        logger.warning("risky_recovery_blocked", service=service_key, command=command)


# ── Phase 2: error_log 미해결 건 스캔 ────────────────────────────────────────

async def _phase2_error_scan(conn):
    """미해결 에러 중 복구 가능한 건 자동 처리."""
    rows = await conn.fetch("""
        SELECT id, error_hash, error_type, source, server, message,
               auto_recoverable, recovery_command, occurrence_count
        FROM error_log
        WHERE resolution_type = 'pending'
          AND last_seen > NOW() - INTERVAL '1 hour'
          AND NOT EXISTS (
              SELECT 1 FROM recovery_log rl
              WHERE rl.error_log_id = error_log.id
                AND rl.created_at > NOW() - INTERVAL '10 minutes'
          )
        ORDER BY occurrence_count DESC
        LIMIT 20
    """)

    for err in rows:
        error_type = err["error_type"]
        server = err["server"] or "68"

        # Case 1: 이미 auto_recoverable=True이고 recovery_command가 있으면 실행
        if err["auto_recoverable"] and err["recovery_command"]:
            await _execute_error_recovery(conn, err, err["recovery_command"], server)
            continue

        # Case 2: ERROR_RECOVERY_MAP에서 매칭
        if error_type in ERROR_RECOVERY_MAP:
            cmd_template = ERROR_RECOVERY_MAP[error_type]
            # {service} 치환: source에서 서비스명 추출
            service_name = _extract_service_name(err["source"], err["message"])
            command = cmd_template.replace("{service}", service_name)

            # auto_recoverable 자동 설정
            await conn.execute("""
                UPDATE error_log
                SET auto_recoverable = true, recovery_command = $2
                WHERE id = $1 AND auto_recoverable = false
            """, err["id"], command)

            await _execute_error_recovery(conn, err, command, server)


async def _execute_error_recovery(conn, err: dict, command: str, server: str):
    """error_log 기반 복구 실행."""
    error_key = f"error:{err['id']}"

    if _circuit_open(error_key):
        return

    # 최대 복구 시도 횟수 제한 (5회 초과 시 manual_required로 전환)
    attempt_count = await conn.fetchval(
        "SELECT COUNT(*) FROM recovery_log WHERE error_log_id = $1",
        err["id"],
    )
    if attempt_count >= 5:
        await conn.execute("""
            UPDATE error_log
            SET resolution_type='manual_required',
                resolution='Auto-recovery failed after 5 attempts'
            WHERE id=$1 AND resolution_type='pending'
        """, err["id"])
        logger.warning("error_max_retries_exceeded", error_id=err["id"], attempts=attempt_count)
        return

    if _is_safe_command(command):
        result = await _execute_command(command, server)

        # recovery_log에 기록 (기존 스키마 호환)
        try:
            await conn.execute("""
                INSERT INTO recovery_log (error_log_id, recovery_command, success, output)
                VALUES ($1, $2, $3, $4)
            """, err["id"], command, result["success"], result["output"][:500])
        except Exception:
            pass

        if result["success"]:
            _circuit_record_success(error_key)
            # error_log resolved 처리
            await conn.execute("""
                UPDATE error_log
                SET resolution_type='auto', resolution=$2, resolved_at=NOW()
                WHERE id=$1 AND resolution_type='pending'
            """, err["id"], f"Auto-recovered: {command}")

            await _notify_telegram(
                f"\u2705 *\uc5d0\ub7ec \uc790\ub3d9\ubcf5\uad6c*\n"
                f"\ud0c0\uc785: {err['error_type']}\n"
                f"\uba85\ub839: `{command}`\n"
                f"\uacb0\uacfc: \uc131\uacf5"
            )
            logger.info("error_auto_recovered", error_id=err["id"], command=command)
        else:
            _circuit_record_failure(error_key)
            logger.warning("error_recovery_failed", error_id=err["id"], output=result["output"][:200])
    else:
        # 안전하지 않은 명령 → 승인 요청
        await _create_approval_request(
            conn,
            title=f"\uc5d0\ub7ec \ubcf5\uad6c: {err['error_type']}",
            description=f"{err['message'][:300]}\n\ubc1c\uc0dd {err['occurrence_count']}\ud68c",
            command=command,
            target_server=server,
            severity="high" if err["occurrence_count"] >= 5 else "medium",
            error_log_id=err["id"],
        )


def _extract_service_name(source: str, message: str) -> str:
    """에러 source/message에서 서비스명 추출."""
    # "aads-server", "aads-postgres" 등 Docker 컨테이너명 매칭
    import re
    for pattern in [r'(aads-\w+)', r'(go100-\w+)', r'(newtalk-\w+)']:
        m = re.search(pattern, f"{source} {message}")
        if m:
            return m.group(1)
    # source 자체가 서비스명일 수 있음
    if source and not source.startswith("/"):
        return source.split("/")[0].split(":")[0]
    return "aads-server"


# ── Phase 3: 알림 자동 Acknowledge ────────────────────────────────────────────

async def _phase3_alert_auto_acknowledge(conn):
    """해소된 조건의 알림 자동 acknowledge."""
    try:
        import shutil
        disk_usage = shutil.disk_usage("/")
        disk_pct = disk_usage.used / disk_usage.total * 100

        # 디스크 80% 미만이면 disk_full 알림 자동 해소
        if disk_pct < 78:  # 약간의 히스테리시스 (80% 트리거, 78% 해소)
            await conn.execute("""
                UPDATE alert_history
                SET acknowledged = true, acknowledged_at = NOW()
                WHERE category = 'disk_full' AND acknowledged = false
            """)

        # 메모리 83% 미만이면 memory_high 알림 자동 해소
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            mem_total = mem_available = 0
            for line in lines:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available = int(line.split()[1])
            if mem_total > 0:
                mem_pct = (1 - mem_available / mem_total) * 100
                if mem_pct < 83:
                    await conn.execute("""
                        UPDATE alert_history
                        SET acknowledged = true, acknowledged_at = NOW()
                        WHERE category = 'memory_high' AND acknowledged = false
                    """)
        except Exception:
            pass

        # 24시간 이상 된 cost_exceed 경고 자동 해소 (일일 비용이므로)
        await conn.execute("""
            UPDATE alert_history
            SET acknowledged = true, acknowledged_at = NOW()
            WHERE category = 'cost_exceed'
              AND acknowledged = false
              AND created_at < NOW() - INTERVAL '24 hours'
        """)

        # P4: DB 커넥션 풀 사용률 경고 (80% 초과)
        try:
            from app.core.db_pool import get_pool_stats
            stats = get_pool_stats()
            if stats.get("available") and stats["usage_pct"] >= 80:
                logger.warning("db_pool_high_usage",
                               used=stats["used"], max=stats["max_size"],
                               pct=stats["usage_pct"])
                await conn.execute("""
                    INSERT INTO alert_history (server, category, message, severity)
                    VALUES ('68', 'db_pool_high', $1, 'warning')
                """, f"DB 커넥션 풀 사용률 {stats['usage_pct']}% ({stats['used']}/{stats['max_size']})")
            elif stats.get("available") and stats["usage_pct"] < 70:
                await conn.execute("""
                    UPDATE alert_history
                    SET acknowledged = true, acknowledged_at = NOW()
                    WHERE category = 'db_pool_high' AND acknowledged = false
                """)
        except Exception as _pe:
            logger.debug("pool_check_error", error=str(_pe))

    except Exception as e:
        logger.debug("alert_auto_ack_error", error=str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  초기화 + 상태 조회
# ══════════════════════════════════════════════════════════════════════════════

_initialized = False


async def initialize():
    """최초 1회 실행. recovery_log 시드 데이터의 auto_executable 확인."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    logger.info("unified_healer_initialized")


async def get_healer_status() -> dict:
    """힐러 상태 조회 (디버깅/대시보드용)."""
    try:
        conn = await _get_conn()
        try:
            pending_errors = await conn.fetchval(
                "SELECT count(*) FROM error_log WHERE resolution_type='pending'"
            )
            auto_resolved = await conn.fetchval(
                "SELECT count(*) FROM error_log WHERE resolution_type='auto'"
            )
            pending_approvals = await conn.fetchval(
                "SELECT count(*) FROM approval_queue WHERE status='pending'"
            )
            healthy_services = await conn.fetchval(
                "SELECT count(*) FROM monitored_services WHERE enabled=true AND last_status='ok'"
            )
            total_services = await conn.fetchval(
                "SELECT count(*) FROM monitored_services WHERE enabled=true"
            )
            unack_alerts = await conn.fetchval(
                "SELECT count(*) FROM alert_history WHERE acknowledged=false"
            )
            return {
                "status": "running",
                "circuit_breakers_open": sum(
                    1 for k, v in _circuit_breaker.items()
                    if v["failures"] >= CIRCUIT_BREAKER_MAX_FAILURES
                ),
                "pending_errors": pending_errors,
                "auto_resolved": auto_resolved,
                "pending_approvals": pending_approvals,
                "healthy_services": f"{healthy_services}/{total_services}",
                "unacknowledged_alerts": unack_alerts,
            }
        finally:
            await conn.close()
    except Exception as e:
        return {"status": "error", "error": str(e)}
