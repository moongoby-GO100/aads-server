-- 091: Pipeline Runner reliability status normalization
-- Date: 2026-05-13
--
-- Goals:
-- - Treat no_changes/dedup_blocked/blocked_dependency and common runner
--   failure classes as first-class dashboard/API states.
-- - Preserve one active job per project + instruction_hash + parallel_group
--   scope while allowing explicit different parallel groups.
-- - Keep blocked rows terminal and non-retryable in review_feedback/logs.

UPDATE pipeline_jobs
SET
    status = 'cancelled',
    phase = 'no_changes',
    error_detail = 'no_changes',
    review_feedback = COALESCE(review_feedback, '')
        || E'\n[Runner Guard] no_changes is terminal complete; auto_retryable=false',
    updated_at = NOW()
WHERE (phase = 'no_changes' OR error_detail = 'no_changes')
  AND (status IS DISTINCT FROM 'cancelled' OR phase IS DISTINCT FROM 'no_changes');

UPDATE pipeline_jobs
SET
    status = 'cancelled',
    phase = 'dedup_blocked',
    error_detail = COALESCE(NULLIF(error_detail, ''), 'dedup_blocked: existing active job'),
    review_feedback = COALESCE(review_feedback, '')
        || E'\n[Runner Guard] dedup_blocked is terminal blocked; auto_retryable=false',
    updated_at = NOW()
WHERE (phase = 'dedup_blocked' OR error_detail LIKE 'dedup_blocked%')
  AND (status IS DISTINCT FROM 'cancelled' OR phase IS DISTINCT FROM 'dedup_blocked');

UPDATE pipeline_jobs
SET
    status = 'cancelled',
    phase = 'blocked_dependency',
    error_detail = COALESCE(NULLIF(error_detail, ''), 'blocked_dependency: upstream not runnable'),
    review_feedback = COALESCE(review_feedback, '')
        || E'\n[Runner Guard] blocked_dependency is terminal blocked; auto_retryable=false',
    updated_at = NOW()
WHERE (phase = 'blocked_dependency' OR error_detail LIKE 'blocked_dependency%' OR error_detail LIKE 'orphaned_dependency%')
  AND (status IS DISTINCT FROM 'cancelled' OR phase IS DISTINCT FROM 'blocked_dependency');

UPDATE pipeline_jobs
SET phase = CASE
        WHEN error_detail LIKE 'build_fail%' THEN 'build_fail'
        WHEN error_detail LIKE 'deploy_failed%' THEN 'deploy_failed'
        WHEN error_detail LIKE 'review_failed%' THEN 'review_failed'
        WHEN error_detail LIKE 'auth_unavailable%' THEN 'auth_unavailable'
        WHEN error_detail LIKE 'tool_timeout%' THEN 'tool_timeout'
        ELSE phase
    END,
    updated_at = NOW()
WHERE error_detail LIKE 'build_fail%'
   OR error_detail LIKE 'deploy_failed%'
   OR error_detail LIKE 'review_failed%'
   OR error_detail LIKE 'auth_unavailable%'
   OR error_detail LIKE 'tool_timeout%';

WITH ranked AS (
    SELECT
        job_id,
        project,
        instruction_hash,
        COALESCE(parallel_group, '') AS scope_key,
        row_number() OVER (
            PARTITION BY project, instruction_hash, COALESCE(parallel_group, '')
            ORDER BY
                CASE status
                    WHEN 'running' THEN 0
                    WHEN 'claimed' THEN 1
                    WHEN 'awaiting_approval' THEN 2
                    WHEN 'approved' THEN 3
                    WHEN 'deploying' THEN 4
                    WHEN 'rolling_back' THEN 5
                    WHEN 'queued' THEN 6
                    ELSE 7
                END,
                created_at ASC
        ) AS rn
    FROM pipeline_jobs
    WHERE instruction_hash IS NOT NULL
      AND status IN (
          'queued',
          'claimed',
          'running',
          'awaiting_approval',
          'approved',
          'deploying',
          'rolling_back'
      )
)
UPDATE pipeline_jobs pj
SET
    status = 'cancelled',
    phase = 'dedup_blocked',
    error_detail = 'dedup_blocked: duplicate active project/instruction_hash/scope',
    review_feedback = COALESCE(pj.review_feedback, '')
        || E'\n[Runner Guard] 동일 project+instruction_hash+parallel_group active 작업이 있어 자동 차단됨; auto_retryable=false',
    updated_at = NOW()
FROM ranked r
WHERE pj.job_id = r.job_id
  AND r.rn > 1;

DROP INDEX IF EXISTS uq_pipeline_jobs_active_instruction_hash;

CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_jobs_active_instruction_hash_scope
    ON pipeline_jobs (project, instruction_hash, COALESCE(parallel_group, ''))
    WHERE instruction_hash IS NOT NULL
      AND status IN (
          'queued',
          'claimed',
          'running',
          'awaiting_approval',
          'approved',
          'deploying',
          'rolling_back'
      );

