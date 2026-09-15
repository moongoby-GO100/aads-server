"""
AADS-191: Pipeline Jobs 자동 정리 + 중복 병합.

1시간마다 실행:
- 1시간 이상 된 종료 작업(done/error/cancelled/rejected/rejected_done)을
  pipeline_jobs_archive 로 이관 (2026-09-15부터 삭제 아님)
- 보관 기간(180일) 지난 아카이브 행 삭제
- 동일 project+instruction_hash로 approved가 2개 이상이면 최신 1개만 남기고 나머지 취소
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


# 종료 상태 전부. 'rejected_done' 이 빠져 있어서 2026-04-02 부터 710건이
# 큐 테이블에 그대로 쌓여 있었다 (2026-09-15 실측). 하나라도 빠지면 다시 샌다.
_TERMINAL_STATUSES = ("done", "error", "cancelled", "rejected", "rejected_done")

# 아카이브 보관 기간. 이력 집계(성공률·소요시간)는 이 범위 안에서 낸다.
_ARCHIVE_RETENTION_DAYS = 180


async def cleanup_stale_jobs() -> dict:
    """1시간 이상 된 종료 작업을 아카이브로 옮긴다(삭제 아님).

    예전에는 그냥 DELETE 했다. 그래서 성공한 작업은 1시간 뒤 흔적이 없어졌고
    프로젝트별 성공률·소요시간을 pipeline_jobs 로 낼 수가 없었다(ACCT 는 전 기간 0건).
    큐 테이블을 작게 유지하려는 원래 목적은 그대로 두고, 행만 아카이브로 옮긴다.
    """
    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        # DELETE ... RETURNING 을 CTE 로 감싸 한 문장으로 옮긴다 — 중간에 끊겨도
        # 옮기지 않은 채 지워지는 일이 없다.
        # git_diff/logs/result_output 은 용량이 커서 뺀다 (diff 는 git 에서 복원된다).
        result = await conn.execute(
            """
            WITH moved AS (
                DELETE FROM pipeline_jobs
                WHERE status = ANY($1::text[])
                  AND updated_at < NOW() - INTERVAL '1 hour'
                RETURNING *
            )
            INSERT INTO pipeline_jobs_archive
                (job_id, project, status, runner_host, created_at, updated_at, row_data)
            SELECT job_id, project, status, runner_host, created_at, updated_at,
                   to_jsonb(moved) - 'git_diff' - 'logs' - 'result_output'
            FROM moved
            ON CONFLICT (job_id) DO NOTHING
            """,
            list(_TERMINAL_STATUSES),
        )

    archived = int(result.split()[-1]) if result else 0
    if archived > 0:
        logger.info("pipeline_cleanup.stale_archived", count=archived)
    # 호출부 호환을 위해 키 이름은 유지한다 (큐에서 빠진 건수라는 뜻은 같다).
    return {"deleted": archived, "archived": archived}


async def purge_old_archive() -> dict:
    """보관 기간이 지난 아카이브 행 삭제 — 아카이브가 무한히 크지 않게."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""
            DELETE FROM pipeline_jobs_archive
            WHERE archived_at < NOW() - INTERVAL '{_ARCHIVE_RETENTION_DAYS} days'
            """
        )

    purged = int(result.split()[-1]) if result else 0
    if purged > 0:
        logger.info("pipeline_cleanup.archive_purged", count=purged)
    return {"purged": purged}


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
        purged = await purge_old_archive()
        total = stale["deleted"] + merged["cancelled"]
        if total > 0:
            logger.info("pipeline_cleanup.done",
                         archived=stale["deleted"], merged=merged["cancelled"],
                         purged=purged["purged"])
        return {
            "deleted": stale["deleted"],
            "archived": stale["deleted"],
            "merged": merged["cancelled"],
            "purged": purged["purged"],
        }
    except Exception as e:
        logger.warning("pipeline_cleanup.error", error=str(e))
        return {"error": str(e)}
