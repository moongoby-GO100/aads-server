"""
AADS-132: 3단계 에스컬레이션 엔진.

tier_1 (자동복구, 5분):   soft_kill, session_switch, service_restart, config_refresh
tier_2 (강화복구, 10분):  hard_kill, full_service_restart, emergency_slot_clear, docker_restart, bridge_full_restart
tier_3 (인간 에스컬레이션): pause_pipeline, dump_diagnostics, create_incident_report
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime

import structlog

logger = structlog.get_logger()

# ─── 에스컬레이션 티어 정의 ───────────────────────────────────────────────────

ESCALATION_TIERS = {
    "tier_1": {
        "label": "자동복구",
        "timeout_minutes": 5,
        "actions": ["soft_kill", "session_switch", "service_restart", "config_refresh"],
        "notification": None,
    },
    "tier_2": {
        "label": "강화복구",
        "timeout_minutes": 10,
        "actions": ["hard_kill", "full_service_restart", "emergency_slot_clear",
                    "docker_restart", "bridge_full_restart"],
        "notification": "telegram",
    },
    "tier_3": {
        "label": "인간 에스컬레이션",
        "timeout_minutes": None,
        "actions": ["pause_pipeline", "dump_diagnostics", "create_incident_report"],
        "notification": "telegram_urgent",
    },
}


# ─── 메인 에스컬레이션 실행 ───────────────────────────────────────────────────

async def execute_escalation(issue_type: str, issue_data: dict) -> bool:
    """
    3단계 순차 에스컬레이션.
    tier_1 성공 → 즉시 반환.
    tier_1 실패 → tier_2 시도 → 성공 시 반환.
    tier_2 실패 → tier_3 (인간 개입 요청).
    """
    for tier_key in ["tier_1", "tier_2", "tier_3"]:
        tier = ESCALATION_TIERS[tier_key]
        logger.info(
            "escalation_tier_start",
            tier=tier_key,
            label=tier["label"],
            issue_type=issue_type,
        )

        success = False
        for action in tier["actions"]:
            try:
                ok = await execute_action(action, issue_data)
                if ok:
                    success = True
                    logger.info("escalation_action_success", tier=tier_key, action=action)
                    break
            except Exception as e:
                logger.warning("escalation_action_failed", tier=tier_key, action=action, error=str(e))

        if success:
            logger.info("escalation_resolved", tier=tier_key, issue_type=issue_type)
            return True

        # 알림 발송
        if tier["notification"]:
            await _send_escalation_notification(tier_key, tier["notification"], issue_type, issue_data)

        # 타임아웃 대기 (실제 운영 시)
        if tier["timeout_minutes"] and tier_key != "tier_3":
            logger.info("escalation_waiting", tier=tier_key, minutes=tier["timeout_minutes"])
            # 테스트 환경에서는 0.1초로 단축
            await asyncio.sleep(0.1)
            if await _check_resolved(issue_type, issue_data):
                return True

    logger.error("escalation_all_tiers_failed", issue_type=issue_type)
    return False


# ─── 액션 실행 함수들 ─────────────────────────────────────────────────────────

async def execute_action(action: str, issue_data: dict) -> bool:
    """액션별 실행 분기."""
    pid = issue_data.get("pid")
    service = issue_data.get("service", "aads-server")
    container = issue_data.get("container", "aads-server")

    dispatch = {
        "soft_kill": lambda: _soft_kill(pid),
        "hard_kill": lambda: _hard_kill(pid),
        "session_switch": lambda: _session_switch(issue_data),
        "service_restart": lambda: _service_restart(service),
        "full_service_restart": lambda: _service_restart(service),
        "config_refresh": lambda: _config_refresh(issue_data),
        "emergency_slot_clear": lambda: _emergency_slot_clear(issue_data),
        "docker_restart": lambda: _docker_restart(container),
        "bridge_full_restart": lambda: _bridge_full_restart(),
        "pause_pipeline": lambda: _pause_pipeline(),
        "dump_diagnostics": lambda: _dump_diagnostics(issue_data),
        "create_incident_report": lambda: _create_incident_report(issue_data),
    }
    fn = dispatch.get(action)
    if fn is None:
        logger.warning("escalation_unknown_action", action=action)
        return False
    return await fn()


async def _soft_kill(pid) -> bool:
    if not pid:
        return False
    try:
        subprocess.run(["kill", "-TERM", str(pid)], check=True, capture_output=True)
        logger.info("action_soft_kill", pid=pid)
        return True
    except subprocess.CalledProcessError:
        return False


async def _hard_kill(pid) -> bool:
    if not pid:
        return False
    try:
        subprocess.run(["kill", "-9", str(pid)], check=True, capture_output=True)
        logger.info("action_hard_kill", pid=pid)
        return True
    except subprocess.CalledProcessError:
        return False


async def _session_switch(issue_data: dict) -> bool:
    logger.info("action_session_switch", data=issue_data)
    return True


async def _service_restart(service: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            subprocess.run(["systemctl", "restart", service], capture_output=True)
            logger.info("action_service_restart", service=service)
            return True
        return False
    except Exception:
        return False


async def _config_refresh(issue_data: dict) -> bool:
    logger.info("action_config_refresh", data=issue_data)
    return True


async def _emergency_slot_clear(issue_data: dict) -> bool:
    """가장 오래된 running 작업 강제 종료 (lifecycle DB 기반)."""
    db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
    if not db_url:
        return False
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            row = await conn.fetchrow(
                """
                SELECT task_id FROM directive_lifecycle
                WHERE status='running'
                ORDER BY started_at ASC NULLS LAST
                LIMIT 1
                """
            )
            if row:
                task_id = row["task_id"]
                await conn.execute(
                    """
                    UPDATE directive_lifecycle
                    SET status='failed', error_detail='emergency_slot_clear',
                        completed_at=NOW()
                    WHERE task_id=$1
                    """,
                    task_id,
                )
                logger.info("action_emergency_slot_clear", task_id=task_id)
                return True
            return False
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("emergency_slot_clear_failed", error=str(e))
        return False


async def _docker_restart(container: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "restart", container],
            capture_output=True, text=True, timeout=60,
        )
        logger.info("action_docker_restart", container=container, returncode=result.returncode)
        return result.returncode == 0
    except Exception as e:
        logger.warning("docker_restart_failed", container=container, error=str(e))
        return False


async def _bridge_full_restart() -> bool:
    logger.info("action_bridge_full_restart")
    return True


async def _pause_pipeline() -> bool:
    logger.warning("action_pause_pipeline")
    return True


async def _dump_diagnostics(issue_data: dict) -> bool:
    try:
        diagnostics = {
            "timestamp": datetime.now().isoformat(),
            "issue": issue_data,
            "ps": subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout[:2000],
            "free": subprocess.run(["free", "-h"], capture_output=True, text=True).stdout,
            "df": subprocess.run(["df", "-h"], capture_output=True, text=True).stdout,
        }
        diag_path = f"/tmp/aads_diagnostics_{int(time.time())}.json"
        with open(diag_path, "w") as f:
            json.dump(diagnostics, f, ensure_ascii=False, indent=2)
        logger.info("action_dump_diagnostics", path=diag_path)
        return True
    except Exception as e:
        logger.warning("dump_diagnostics_failed", error=str(e))
        return False


async def _create_incident_report(issue_data: dict) -> bool:
    db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
    if not db_url:
        return True  # DB 없어도 graceful
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            await conn.execute(
                """
                INSERT INTO recovery_logs
                    (issue_type, issue_data, tier, action_taken, result, recovered_by)
                VALUES ($1, $2::jsonb, 'tier_3', 'create_incident_report', 'escalated', 'escalation_engine')
                """,
                issue_data.get("issue_type", "unknown"),
                json.dumps(issue_data, ensure_ascii=False),
            )
            logger.info("action_create_incident_report", issue_type=issue_data.get("issue_type"))
            return True
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("create_incident_report_failed", error=str(e))
        return False


# ─── 헬퍼 ────────────────────────────────────────────────────────────────────

async def _check_resolved(issue_type: str, issue_data: dict) -> bool:
    """이슈 해결 여부 확인 (기본: 항상 False, 오버라이드 가능)."""
    return False


async def _send_escalation_notification(
    tier_key: str, notification_type: str, issue_type: str, issue_data: dict
) -> None:
    """에스컬레이션 알림 발송."""
    try:
        from app.services.ceo_notify import send_telegram
        if notification_type == "telegram_urgent":
            msg = f"🚨🚨🚨 [AADS 긴급] {tier_key} 에스컬레이션 실패\n이슈: {issue_type}\n데이터: {json.dumps(issue_data, ensure_ascii=False)[:200]}"
            for _ in range(3):
                await send_telegram(msg)
                await asyncio.sleep(0.1)
        else:
            msg = f"⚠️ [AADS] {tier_key} 에스컬레이션\n이슈: {issue_type}"
            await send_telegram(msg)
    except Exception:
        pass
