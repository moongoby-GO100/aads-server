"""
AADS-191: Pipeline Jobs 자동 정리 + 중복 병합.

1시간마다 실행:
- 1시간 이상 된 done/error/cancelled/rejected 작업 삭제
- 동일 project+instruction_hash로 approved가 2개 이상이면 최신 1개만 남기고 나머지 취소
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


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
            result = await conn.execute(
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
                """,
                dup["project"], dup["instruction_hash"],
            )
            affected = int(result.split()[-1]) if result else 0
            cancelled += affected

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
