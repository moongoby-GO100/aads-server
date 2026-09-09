"""
AADS-191: Pipeline Jobs 자동 정리 + 중복 병합.

1시간마다 실행:
- 1시간 이상 된 done/error/cancelled/rejected 작업 삭제
- 동일 project+instruction_hash로 approved가 2개 이상이면 최신 1개만 남기고 나머지 취소
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


async def _reconcile_merged_job(job_id: str, kept_job_id: str | None) -> None:
    """중복 병합으로 취소된 작업의 목표 링크를 정리한다 (best-effort).

    ① 취소 상태를 링크에 반영하고 ② 남긴 작업이 대체했음을 승계로 기록한다.
    ②가 없으면 병합된 옛 작업이 'failed' 로만 남아 마일스톤을 영구히 막는다.
    """
    try:
        from app.services.goal_link_reconciler import mark_superseded, sync_job_status

        await sync_job_status(job_id, "cancelled", "cancelled", source="pipeline_cleanup_merge")
        if kept_job_id:
            await mark_superseded(job_id, kept_job_id, "duplicate_approved_merge")
    except Exception as exc:  # noqa: BLE001 — 정리 작업이 목표 반영 때문에 멈추면 안 된다
        logger.warning("pipeline_cleanup.goal_link_sync_failed", job_id=job_id, error=str(exc))


async def cleanup_stale_jobs() -> dict:
    """1시간 이상 된 완료/에러 작업 삭제."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM pipeline_jobs
            WHERE status IN ('done', 'error', 'cancelled', 'rejected')
              AND updated_at < NOW() - INTERVAL '1 hour'
            """
        )

    deleted = int(result.split()[-1]) if result else 0
    if deleted > 0:
        logger.info("pipeline_cleanup.stale_deleted", count=deleted)
    return {"deleted": deleted}


async def merge_duplicate_approved() -> dict:
    """동일 project+instruction_hash로 approved가 2개 이상이면 최신 1개만 남기고 나머지 cancelled."""
    # (헬퍼는 아래 _reconcile_merged_job — 목표 링크 상태/승계 반영, best-effort)
    from app.core.db_pool import get_pool
    pool = get_pool()

    cancelled = 0
    async with pool.acquire() as conn:
        # 중복 approved 그룹 찾기
        dups = await conn.fetch(
            """
            SELECT project, instruction_hash, count(*) as cnt
            FROM pipeline_jobs
            WHERE status = 'approved'
              AND instruction_hash IS NOT NULL
            GROUP BY project, instruction_hash
            HAVING count(*) > 1
            """
        )

        for dup in dups:
            # 최신 1개만 남기고 나머지 cancelled 처리
            kept_job_id = await conn.fetchval(
                """
                SELECT job_id FROM pipeline_jobs
                WHERE project = $1 AND instruction_hash = $2 AND status = 'approved'
                ORDER BY created_at DESC LIMIT 1
                """,
                dup["project"], dup["instruction_hash"],
            )
            merged = await conn.fetch(
                """
                UPDATE pipeline_jobs
                SET status = 'cancelled', updated_at = NOW()
                WHERE project = $1
                  AND instruction_hash = $2
                  AND status = 'approved'
                  AND job_id NOT IN (
                    SELECT job_id FROM pipeline_jobs
                    WHERE project = $1
                      AND instruction_hash = $2
                      AND status = 'approved'
                    ORDER BY created_at DESC
                    LIMIT 1
                  )
                RETURNING job_id
                """,
                dup["project"], dup["instruction_hash"],
            )
            cancelled += len(merged)
            # 중복 병합은 "누가 누구를 대체했는지"가 그 자리에서 확정된다.
            # 목표 링크에 종료 상태 + 승계를 같이 남겨야, 병합된 옛 작업이
            # 마일스톤을 실패로 영구히 막지 않는다.
            for row in merged:
                await _reconcile_merged_job(row["job_id"], kept_job_id)

    if cancelled > 0:
        logger.info("pipeline_cleanup.duplicates_merged", cancelled=cancelled)
    return {"cancelled": cancelled}


async def run_pipeline_cleanup() -> dict:
    """전체 정리 실행 (스케줄러에서 호출)."""
    try:
        stale = await cleanup_stale_jobs()
        merged = await merge_duplicate_approved()
        total = stale["deleted"] + merged["cancelled"]
        if total > 0:
            logger.info("pipeline_cleanup.done",
                         deleted=stale["deleted"], merged=merged["cancelled"])
        return {"deleted": stale["deleted"], "merged": merged["cancelled"]}
    except Exception as e:
        logger.warning("pipeline_cleanup.error", error=str(e))
        return {"error": str(e)}
