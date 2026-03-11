"""
AADS-134: ProjectHealingConfig — AADS SaaS 프로젝트 자기치유 자동 적용
향후 생성되는 모든 고객 프로젝트에 4계층 자기치유가 자동으로 적용된다.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import asyncpg
import structlog

from app.services.escalation_engine import execute_escalation
from app.services.recovery_graph import execute_recovery_chain

logger = structlog.get_logger()

KST = timezone(timedelta(hours=9))

# ─── Default Healing Config ───────────────────────────────────────────────────

DEFAULT_HEALING_CONFIG: Dict[str, Any] = {
    "hard_timeout": 1800,             # L1: 30분 프로세스 타임아웃
    "max_retries": 3,                 # L2: 최대 재시도 횟수
    "escalation_enabled": True,       # L2/L3: 에스컬레이션 활성화
    "circuit_breaker_threshold": 3,   # L2: 3회 연속 실패 → 서킷브레이커 오픈
    "health_check_interval": 30,      # L2: 헬스체크 주기 (초)
    "cooldown_seconds": 300,          # 서킷브레이커 쿨다운 (5분)
    "tier1_action": "self_timeout",   # L1 복구 액션
    "tier2_action": "watchdog_restart",  # L2 복구 액션
    "tier3_action": "meta_watchdog",  # L3 복구 액션
}


@dataclass
class ProjectHealingConfig:
    """프로젝트별 자기치유 설정 — 생성 시 자동 부여."""

    project_id: str
    hard_timeout: int = 1800
    max_retries: int = 3
    escalation_enabled: bool = True
    circuit_breaker_threshold: int = 3
    health_check_interval: int = 30
    cooldown_seconds: int = 300
    tier1_action: str = "self_timeout"
    tier2_action: str = "watchdog_restart"
    tier3_action: str = "meta_watchdog"

    # 런타임 상태
    failure_count: int = 0
    circuit_open: bool = False
    cooldown_until: Optional[datetime] = None

    def to_jsonb(self) -> Dict[str, Any]:
        """DB healing_config JSONB 컬럼에 저장할 딕셔너리."""
        return {
            "hard_timeout": self.hard_timeout,
            "max_retries": self.max_retries,
            "escalation_enabled": self.escalation_enabled,
            "circuit_breaker_threshold": self.circuit_breaker_threshold,
            "health_check_interval": self.health_check_interval,
            "cooldown_seconds": self.cooldown_seconds,
            "tier1_action": self.tier1_action,
            "tier2_action": self.tier2_action,
            "tier3_action": self.tier3_action,
        }

    @classmethod
    def from_jsonb(cls, project_id: str, data: Optional[Dict[str, Any]]) -> "ProjectHealingConfig":
        """DB JSONB에서 복원."""
        if not data:
            return cls(project_id=project_id)
        return cls(
            project_id=project_id,
            hard_timeout=data.get("hard_timeout", 1800),
            max_retries=data.get("max_retries", 3),
            escalation_enabled=data.get("escalation_enabled", True),
            circuit_breaker_threshold=data.get("circuit_breaker_threshold", 3),
            health_check_interval=data.get("health_check_interval", 30),
            cooldown_seconds=data.get("cooldown_seconds", 300),
            tier1_action=data.get("tier1_action", "self_timeout"),
            tier2_action=data.get("tier2_action", "watchdog_restart"),
            tier3_action=data.get("tier3_action", "meta_watchdog"),
        )


# ─── Healing Engine ───────────────────────────────────────────────────────────

class ProjectHealingEngine:
    """프로젝트 실행 시 자기치유 로직 적용."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._configs: Dict[str, ProjectHealingConfig] = {}

    async def _get_conn(self) -> asyncpg.Connection:
        return await asyncpg.connect(self.database_url, timeout=10)

    async def get_config(self, project_id: str) -> ProjectHealingConfig:
        """projects 테이블에서 healing_config 조회."""
        if project_id in self._configs:
            return self._configs[project_id]
        try:
            conn = await self._get_conn()
            try:
                row = await conn.fetchrow(
                    "SELECT healing_config FROM projects WHERE project_id=$1",
                    project_id
                )
            finally:
                await conn.close()
            if row and row["healing_config"]:
                cfg = ProjectHealingConfig.from_jsonb(project_id, dict(row["healing_config"]))
            else:
                cfg = ProjectHealingConfig(project_id=project_id)
            self._configs[project_id] = cfg
            return cfg
        except Exception as e:
            logger.warning("project_healing_config_load_error", project_id=project_id, error=str(e))
            return ProjectHealingConfig(project_id=project_id)

    async def on_project_created(self, project_id: str, conn: asyncpg.Connection) -> None:
        """프로젝트 생성 시 기본 healing_config 자동 부여."""
        cfg = ProjectHealingConfig(project_id=project_id)
        await conn.execute(
            "UPDATE projects SET healing_config=$1 WHERE project_id=$2",
            cfg.to_jsonb(),
            project_id,
        )
        logger.info("project_healing_config_set", project_id=project_id)

    async def apply_l1_timer(self, project_id: str, task_id: str, coro) -> Any:
        """L1: hard_timeout 타이머 자동 적용. exit code 124(timeout) 시 복구 트리거."""
        cfg = await self.get_config(project_id)
        try:
            result = await asyncio.wait_for(coro, timeout=cfg.hard_timeout)
            await self._on_task_success(project_id, task_id)
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "project_l1_timeout",
                project_id=project_id,
                task_id=task_id,
                timeout=cfg.hard_timeout,
            )
            await self._record_recovery_log(
                project_id=project_id,
                task_id=task_id,
                issue_type="l1_timeout",
                tier="L1",
                action="hard_timeout_kill",
                result="escalated",
            )
            await self._run_recovery_and_escalation(
                project_id=project_id,
                task_id=task_id,
                issue_type="task_timeout",
                detail="L1 hard_timeout",
            )
            raise
        except Exception as e:
            await self._on_task_failure(project_id, task_id, str(e))
            raise

    async def _on_task_success(self, project_id: str, task_id: str) -> None:
        """성공 시 서킷브레이커 failure_count 리셋."""
        cfg = await self.get_config(project_id)
        if cfg.failure_count > 0:
            cfg.failure_count = 0
            cfg.circuit_open = False
            cfg.cooldown_until = None
            logger.info("project_healing_reset", project_id=project_id, task_id=task_id)

    async def _on_task_failure(self, project_id: str, task_id: str, error: str) -> None:
        """실패 시 서킷브레이커 상태 업데이트 + 복구 이력 기록."""
        cfg = await self.get_config(project_id)
        cfg.failure_count += 1

        # 에스컬레이션 결정
        if cfg.failure_count <= cfg.max_retries:
            tier = "L2"
            action = cfg.tier2_action
            result = "escalated"
        else:
            tier = "L3"
            action = cfg.tier3_action
            result = "escalated"

        # 서킷브레이커 오픈
        if cfg.failure_count >= cfg.circuit_breaker_threshold:
            cfg.circuit_open = True
            cfg.cooldown_until = datetime.now(KST) + timedelta(seconds=cfg.cooldown_seconds)
            logger.warning(
                "project_circuit_breaker_open",
                project_id=project_id,
                failure_count=cfg.failure_count,
                cooldown_until=cfg.cooldown_until.isoformat(),
            )

        await self._record_recovery_log(
            project_id=project_id,
            task_id=task_id,
            issue_type="task_failure",
            tier=tier,
            action=action,
            result=result,
            detail=error,
        )
        await self._run_recovery_and_escalation(
            project_id=project_id,
            task_id=task_id,
            issue_type="task_failure",
            detail=error,
        )

    def is_circuit_open(self, project_id: str) -> bool:
        """서킷브레이커가 열려 있으면 해당 프로젝트 작업 투입 차단."""
        cfg = self._configs.get(project_id)
        if not cfg or not cfg.circuit_open:
            return False
        if cfg.cooldown_until and datetime.now(KST) > cfg.cooldown_until:
            cfg.circuit_open = False
            cfg.failure_count = 0
            cfg.cooldown_until = None
            logger.info("project_circuit_breaker_auto_reset", project_id=project_id)
            return False
        return True

    async def _record_recovery_log(
        self,
        project_id: str,
        task_id: str,
        issue_type: str,
        tier: str,
        action: str,
        result: str,
        duration_seconds: Optional[float] = None,
        detail: Optional[str] = None,
    ) -> None:
        """escalation_recovery 테이블에 project_id 포함 기록."""
        try:
            conn = await self._get_conn()
            try:
                await conn.execute(
                    """
                    INSERT INTO escalation_recovery
                        (issue_type, affected_server, affected_task, tier,
                         action_taken, result, duration_seconds, detail, project_id, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                    """,
                    issue_type,
                    "saas",
                    task_id,
                    tier,
                    action,
                    result,
                    duration_seconds,
                    detail,
                    project_id,
                )
            finally:
                await conn.close()
            logger.info(
                "recovery_log_recorded",
                project_id=project_id,
                task_id=task_id,
                issue_type=issue_type,
                result=result,
            )
        except Exception as e:
            logger.error("recovery_log_insert_error", error=str(e))

    async def _run_recovery_and_escalation(
        self,
        project_id: str,
        task_id: str,
        issue_type: str,
        detail: Optional[str] = None,
    ) -> None:
        issue = {
            "issue_type": issue_type,
            "project_id": project_id,
            "task_id": task_id,
            "server": "68",
            "detail": detail,
        }
        try:
            await execute_recovery_chain([issue])
        except Exception as e:
            logger.warning("project_healing_recovery_graph_failed", error=str(e))
        try:
            await execute_escalation(issue_type, issue)
        except Exception as e:
            logger.warning("project_healing_escalation_failed", error=str(e))


# ─── DB Migration Helper ──────────────────────────────────────────────────────

ALTER_TABLE_SQL = """
ALTER TABLE projects ADD COLUMN IF NOT EXISTS healing_config JSONB
  DEFAULT '{"hard_timeout":1800,"max_retries":3,"escalation_enabled":true,"circuit_breaker_threshold":3}';
"""


async def run_migration(database_url: str) -> None:
    """projects 테이블에 healing_config 컬럼 추가."""
    conn = await asyncpg.connect(database_url, timeout=10)
    try:
        await conn.execute(ALTER_TABLE_SQL)
        logger.info("project_healing_migration_done")
    finally:
        await conn.close()
