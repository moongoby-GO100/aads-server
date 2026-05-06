-- 078: Pipeline Runner active job deduplication guard
-- Date: 2026-05-06
--
-- Goal:
-- - Prevent duplicate active pipeline_jobs rows for the same instruction_hash.
-- - Keep one active row if historical duplicates already exist, then enforce a
--   partial unique index for queued/running/approval/deploy states.

WITH ranked AS (
    SELECT
        job_id,
        instruction_hash,
        row_number() OVER (
            PARTITION BY instruction_hash
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
    status = 'error',
    phase = 'dedup_blocked',
    error_detail = 'dedup_blocked: duplicate active instruction_hash',
    review_feedback = COALESCE(pj.review_feedback, '')
        || E'\n[중복 차단] 동일 instruction_hash active 작업이 있어 DB 제약 적용 전 자동 정리됨',
    updated_at = NOW()
FROM ranked r
WHERE pj.job_id = r.job_id
  AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_jobs_active_instruction_hash
    ON pipeline_jobs (instruction_hash)
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

