"""
HITL 체크포인트 서비스.
CEO-DIRECTIVES D-010: 6단계 체크포인트 로그 관리.
Phase 1.5: auto_approve=True (자동 승인 모드).
"""
import json
import uuid
from datetime import datetime
from typing import Optional

import structlog

logger = structlog.get_logger()

CHECKPOINT_STAGES = [
    "requirements",     # 1. 요구사항 확인
    "design_review",    # 2. 설계 승인
    "code_review",      # 3. 코드 리뷰
    "test_results",     # 4. 테스트 결과 확인
    "deploy_approval",  # 5. 배포 승인
    "final_review",     # 6. 최종 검수
]


class CheckpointLog:
    """체크포인트 로그 데이터."""
    def __init__(
        self,
        project_id: str,
        stage: str,
        auto_approved: bool = False,
        feedback: str = "",
        metadata: Optional[dict] = None,
    ):
        self.id = str(uuid.uuid4())[:8]
        self.project_id = project_id
        self.stage = stage
        self.auto_approved = auto_approved
        self.feedback = feedback
        self.metadata = metadata or {}
        self.created_at = datetime.utcnow().isoformat()
        self.approved_at: Optional[str] = None

    def approve(self, feedback: str = ""):
        self.approved_at = datetime.utcnow().isoformat()
        self.feedback = feedback or self.feedback
        return self

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "stage": self.stage,
            "auto_approved": self.auto_approved,
            "feedback": self.feedback,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "approved_at": self.approved_at,
        }


# 인메모리 로그 (PostgreSQL 연결 없을 때 fallback)
_checkpoint_logs: dict[str, list[dict]] = {}


async def record_checkpoint(
    project_id: str,
    stage: str,
    auto_approve: bool = True,
    feedback: str = "",
    metadata: Optional[dict] = None,
) -> dict:
    """체크포인트 기록 및 자동/수동 승인 처리.

    Phase 1.5: auto_approve=True이면 즉시 승인 처리.
    체크포인트 로그를 PostgreSQL에 저장 (연결 실패 시 인메모리 fallback).
    """
    log = CheckpointLog(
        project_id=project_id,
        stage=stage,
        auto_approved=auto_approve,
        feedback=feedback,
        metadata=metadata,
    )

    if auto_approve:
        log.approve(feedback="자동 승인 (Phase 1.5 auto_approve mode)")
        logger.info(
            "checkpoint_auto_approved",
            project_id=project_id,
            stage=stage,
        )
    else:
        logger.info(
            "checkpoint_pending_human_review",
            project_id=project_id,
            stage=stage,
        )

    log_dict = log.to_dict()

    # PostgreSQL 저장 시도
    try:
        await _save_to_postgres(log_dict)
    except Exception as e:
        logger.warning(
            "checkpoint_postgres_fallback",
            error=str(e),
            project_id=project_id,
        )
        # 인메모리 fallback
        if project_id not in _checkpoint_logs:
            _checkpoint_logs[project_id] = []
        _checkpoint_logs[project_id].append(log_dict)

    return log_dict


async def _save_to_postgres(log_dict: dict) -> None:
    """PostgreSQL에 체크포인트 로그 저장 (asyncpg 사용)."""
    try:
        import asyncpg
        from app.config import settings

        conn = await asyncpg.connect(settings.SUPABASE_DIRECT_URL, timeout=5)
        try:
            await conn.execute(
                """
                INSERT INTO checkpoint_logs
                    (id, project_id, stage, auto_approved, feedback, metadata, created_at, approved_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                ON CONFLICT (id) DO NOTHING
                """,
                log_dict["id"],
                log_dict["project_id"],
                log_dict["stage"],
                log_dict["auto_approved"],
                log_dict["feedback"],
                json.dumps(log_dict["metadata"]),
                log_dict["created_at"],
                log_dict["approved_at"],
            )
        finally:
            await conn.close()
    except ImportError:
        raise RuntimeError("asyncpg not installed")


async def get_checkpoint_logs(project_id: str) -> list[dict]:
    """프로젝트 체크포인트 로그 조회."""
    # 인메모리 먼저
    logs = _checkpoint_logs.get(project_id, [])
    if logs:
        return logs

    # PostgreSQL 조회 시도
    try:
        import asyncpg
        from app.config import settings

        conn = await asyncpg.connect(settings.SUPABASE_DIRECT_URL, timeout=5)
        try:
            rows = await conn.fetch(
                "SELECT * FROM checkpoint_logs WHERE project_id = $1 ORDER BY created_at",
                project_id,
            )
            return [dict(r) for r in rows]
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("checkpoint_get_failed", error=str(e))
        return logs
