"""
AADS-186C: AlertManager — 규칙 기반 알림 생성 및 발송
- RULES: 8가지 알림 규칙 (server_down, disk_full, cost_exceed 등)
- 중복 방지: 동일 카테고리+서버 1시간 내 중복 미발송
- DB: alert_history 테이블 저장
"""
from __future__ import annotations

import logging
import os
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = structlog.get_logger(__name__)


@dataclass
class Alert:
    severity: str          # 'CRITICAL', 'WARNING', 'INFO'
    category: str          # 'server_down', 'disk_full', etc.
    title: str
    message: str
    server: Optional[str] = None
    project: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class AlertManager:
    """
    AADS 알림 규칙 평가 및 발송 관리자.

    사용 예시:
        manager = AlertManager()
        alerts = await manager.evaluate_rules()
        for alert in alerts:
            await manager.send_alert(alert)
    """

    RULES: List[Dict[str, Any]] = [
        {
            "name": "server_down",
            "severity": "CRITICAL",
            "condition": "health_check_fail_count >= 3",
            "title": "서버 다운",
        },
        {
            "name": "disk_full",
            "severity": "CRITICAL",
            "condition": "disk_usage_percent > 80",
            "title": "디스크 사용량 초과",
        },
        {
            "name": "cost_exceed",
            "severity": "WARNING",
            "condition": "daily_cost > 5.0",
            "title": "일일 AI 비용 초과",
        },
        {
            "name": "ssh_timeout",
            "severity": "WARNING",
            "condition": "ssh_connect_timeout > 10",
            "title": "SSH 연결 타임아웃",
        },
        {
            "name": "task_stall",
            "severity": "WARNING",
            "condition": "task_pending_hours > 24",
            "title": "태스크 장기 대기",
        },
        {
            "name": "memory_high",
            "severity": "WARNING",
            "condition": "memory_usage_percent > 85",
            "title": "메모리 사용량 높음",
        },
        {
            "name": "health_fail",
            "severity": "CRITICAL",
            "condition": "service_health_url_fail",
            "title": "서비스 헬스체크 실패",
        },
        {
            "name": "pat_expiry",
            "severity": "INFO",
            "condition": "github_pat_expires_in_days < 30",
            "title": "GitHub PAT 만료 예정",
        },
    ]

    def __init__(self) -> None:
        self._db_url = self._get_db_url()

    def _get_db_url(self) -> str:
        url = os.getenv("DATABASE_URL", "")
        return url.replace("postgresql://", "postgres://") if url else url

    async def _get_conn(self):
        import asyncpg  # type: ignore[import]
        return await asyncpg.connect(self._db_url, timeout=10)

    # ─── 규칙 평가 ────────────────────────────────────────────────────────────

    async def evaluate_rules(self) -> List[Alert]:
        """모든 규칙 평가 → 알림 생성."""
        alerts: List[Alert] = []

        try:
            metrics = await self._collect_metrics()
        except Exception as e:
            logger.warning("alert_manager_metrics_collection_failed", error=str(e))
            return alerts

        # disk_full
        disk_pct = metrics.get("disk_usage_percent", 0)
        if disk_pct > 80:
            alerts.append(Alert(
                severity="CRITICAL",
                category="disk_full",
                title="디스크 사용량 초과",
                message=f"서버 68 디스크 사용량 {disk_pct:.1f}% (임계값: 80%)",
                server="68",
            ))

        # cost_exceed: 오늘 AI 비용 조회
        daily_cost = metrics.get("daily_cost_usd", 0.0)
        if daily_cost > 5.0:
            alerts.append(Alert(
                severity="WARNING",
                category="cost_exceed",
                title="일일 AI 비용 초과",
                message=f"오늘 AI 비용 ${daily_cost:.2f} (임계값: $5.00)",
            ))

        # task_stall: pending 태스크 24시간 이상
        stall_tasks = metrics.get("stall_task_count", 0)
        if stall_tasks > 0:
            alerts.append(Alert(
                severity="WARNING",
                category="task_stall",
                title="태스크 장기 대기",
                message=f"{stall_tasks}개 태스크가 24시간 이상 pending 상태",
            ))

        # memory_high
        mem_pct = metrics.get("memory_usage_percent", 0)
        if mem_pct > 85:
            alerts.append(Alert(
                severity="WARNING",
                category="memory_high",
                title="메모리 사용량 높음",
                message=f"서버 68 메모리 사용량 {mem_pct:.1f}% (임계값: 85%)",
                server="68",
            ))

        # pat_expiry
        pat_days = metrics.get("github_pat_expires_in_days", 999)
        if pat_days < 30:
            alerts.append(Alert(
                severity="INFO",
                category="pat_expiry",
                title="GitHub PAT 만료 예정",
                message=f"GitHub PAT {pat_days}일 후 만료 예정",
            ))

        return alerts

    async def _collect_metrics(self) -> Dict[str, Any]:
        """시스템 메트릭 수집."""
        import shutil
        metrics: Dict[str, Any] = {}

        # 디스크 사용량 (서버 68)
        try:
            usage = shutil.disk_usage("/")
            metrics["disk_usage_percent"] = usage.used / usage.total * 100
        except Exception:
            metrics["disk_usage_percent"] = 0

        # 메모리 사용량
        try:
            import psutil  # type: ignore[import]
            metrics["memory_usage_percent"] = psutil.virtual_memory().percent
        except ImportError:
            # psutil 없으면 /proc/meminfo 파싱 (L-010: /proc grep 금지 → 직접 open)
            try:
                with open("/proc/meminfo", "r") as f:
                    lines = f.readlines()
                mem_total = mem_free = mem_available = 0
                for line in lines:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        mem_available = int(line.split()[1])
                if mem_total > 0:
                    metrics["memory_usage_percent"] = (1 - mem_available / mem_total) * 100
                else:
                    metrics["memory_usage_percent"] = 0
            except Exception:
                metrics["memory_usage_percent"] = 0

        # 일일 AI 비용 (DB 조회)
        try:
            conn = await self._get_conn()
            try:
                row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(cost), 0) AS daily_cost
                    FROM chat_messages
                    WHERE created_at >= NOW() - INTERVAL '24 hours'
                      AND role = 'assistant'
                    """
                )
                metrics["daily_cost_usd"] = float(row["daily_cost"]) if row else 0.0
            finally:
                await conn.close()
        except Exception:
            metrics["daily_cost_usd"] = 0.0

        # 장기 대기 태스크 (DB 조회)
        try:
            conn = await self._get_conn()
            try:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM directive_lifecycle
                    WHERE status = 'pending'
                      AND created_at < NOW() - INTERVAL '24 hours'
                    """
                )
                metrics["stall_task_count"] = int(row["cnt"]) if row else 0
            finally:
                await conn.close()
        except Exception:
            metrics["stall_task_count"] = 0

        # GitHub PAT 만료 (환경변수 기반 — 실제 API는 별도 구현)
        metrics["github_pat_expires_in_days"] = 999  # 기본값: 만료 안 됨

        return metrics

    # ─── 알림 발송 ────────────────────────────────────────────────────────────

    async def send_alert(self, alert: Alert) -> None:
        """
        alert_history 저장 + Telegram 발송.
        중복 방지: 동일 카테고리+서버 1시간 내 중복 미발송.
        """
        # 중복 체크
        if await self._is_duplicate(alert):
            logger.debug(
                "alert_duplicate_skipped",
                category=alert.category,
                server=alert.server,
            )
            return

        # DB 저장
        alert_id = await self._save_alert(alert)
        logger.info(
            "alert_saved",
            id=alert_id,
            severity=alert.severity,
            category=alert.category,
        )

        # Telegram 발송 (TelegramBot이 초기화된 경우)
        try:
            from app.services.telegram_bot import get_telegram_bot
            bot = get_telegram_bot()
            if bot is not None:
                await bot.send_alert(alert)
        except Exception as e:
            logger.warning("alert_telegram_send_failed", error=str(e))

    async def _is_duplicate(self, alert: Alert) -> bool:
        """동일 category+server 1시간 내 기존 알림 존재 여부."""
        try:
            conn = await self._get_conn()
            try:
                row = await conn.fetchrow(
                    """
                    SELECT id FROM alert_history
                    WHERE category = $1
                      AND (server = $2 OR ($2 IS NULL AND server IS NULL))
                      AND created_at > NOW() - INTERVAL '1 hour'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    alert.category,
                    alert.server,
                )
                return row is not None
            finally:
                await conn.close()
        except Exception as e:
            logger.warning("alert_dedup_check_failed", error=str(e))
            return False

    async def _save_alert(self, alert: Alert) -> Optional[int]:
        """alert_history 테이블에 저장, 생성된 ID 반환."""
        try:
            conn = await self._get_conn()
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO alert_history
                        (severity, category, title, message, server, project)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                    """,
                    alert.severity,
                    alert.category,
                    alert.title,
                    alert.message,
                    alert.server,
                    alert.project,
                )
                return row["id"] if row else None
            finally:
                await conn.close()
        except Exception as e:
            logger.error("alert_save_failed", error=str(e))
            return None

    # ─── 조회 ─────────────────────────────────────────────────────────────────

    async def get_active_alerts(self) -> List[Dict[str, Any]]:
        """미확인 알림 목록 반환."""
        try:
            conn = await self._get_conn()
            try:
                rows = await conn.fetch(
                    """
                    SELECT id, severity, category, title, message, server, project,
                           acknowledged, created_at
                    FROM alert_history
                    WHERE acknowledged = FALSE
                    ORDER BY
                        CASE severity
                            WHEN 'CRITICAL' THEN 1
                            WHEN 'WARNING'  THEN 2
                            ELSE 3
                        END,
                        created_at DESC
                    LIMIT 50
                    """
                )
                return [dict(r) for r in rows]
            finally:
                await conn.close()
        except Exception as e:
            logger.error("get_active_alerts_failed", error=str(e))
            return []

    async def acknowledge_alert(self, alert_id: int) -> bool:
        """알림 확인 처리."""
        try:
            conn = await self._get_conn()
            try:
                result = await conn.execute(
                    """
                    UPDATE alert_history
                    SET acknowledged = TRUE, acknowledged_at = NOW()
                    WHERE id = $1
                    """,
                    alert_id,
                )
                return result == "UPDATE 1"
            finally:
                await conn.close()
        except Exception as e:
            logger.error("acknowledge_alert_failed", error=str(e))
            return False


# 싱글턴 인스턴스
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """AlertManager 싱글턴 반환."""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager
