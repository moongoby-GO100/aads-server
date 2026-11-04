-- 러너 작업 체크포인트: 러너 종료로 대기열에 돌아간 작업의 진행분(diff)을 DB에 저장해 다른 서버도 이어받게 한다.
-- 정리: 7일 지난 행(aads_pipeline_job_checkpoints_prune)과 종료 작업의 행(aads_pipeline_job_checkpoints_purge_terminal)을 지운다.
-- 멱등(IF NOT EXISTS / OR REPLACE). 기준: docs/design/20261104_AADS_러너 재시작 시 작업 이어가기 기획.md 3.1·3.5
--
-- 롤백(앱이 체크포인트를 쓰지 않는 상태에서만):
--   DROP FUNCTION IF EXISTS aads_pipeline_job_checkpoints_purge_terminal();
--   DROP FUNCTION IF EXISTS aads_pipeline_job_checkpoints_prune(interval);
--   DROP TABLE IF EXISTS pipeline_job_checkpoints;

CREATE TABLE IF NOT EXISTS pipeline_job_checkpoints (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           text NOT NULL,
    base_sha         text NOT NULL,
    patch            bytea NOT NULL,
    patch_sha256     text NOT NULL,
    files            jsonb NOT NULL DEFAULT '[]'::jsonb,
    bytes            int NOT NULL,
    model            text,
    attempt          int,
    host             text NOT NULL,
    instruction_hash text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pjc_job_created
    ON pipeline_job_checkpoints (job_id, created_at DESC);

-- 보존 기간이 지난 체크포인트 삭제. 삭제한 행 수를 돌려준다. 기본 7일.
CREATE OR REPLACE FUNCTION aads_pipeline_job_checkpoints_prune(retention interval DEFAULT interval '7 days')
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    n bigint;
BEGIN
    DELETE FROM pipeline_job_checkpoints
     WHERE created_at < now() - retention;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

-- 종료(done/error/cancelled/rejected/rejected_done)된 작업의 체크포인트 삭제. 삭제한 행 수를 돌려준다.
-- 종료 집합은 app/services/pipeline_cleanup.py 의 _TERMINAL_STATUSES 와 같아야 한다(하나라도 빠지면 샌다).
-- 진행 중·대기 작업은 pipeline_jobs 에서 종료 상태가 아니므로 남는다.
-- 종료 작업이 아카이브로 옮겨져 pipeline_jobs 에서 사라진 경우도 아카이브 상태로 판정한다.
-- (pipeline_jobs 에 있는 작업은 아카이브 상태가 아니라 pipeline_jobs 상태가 우선한다.)
CREATE OR REPLACE FUNCTION aads_pipeline_job_checkpoints_purge_terminal()
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    n bigint;
BEGIN
    DELETE FROM pipeline_job_checkpoints c
     WHERE EXISTS (
               SELECT 1 FROM pipeline_jobs j
                WHERE j.job_id = c.job_id
                  AND j.status IN ('done', 'error', 'cancelled', 'rejected', 'rejected_done')
           )
        OR (
               NOT EXISTS (SELECT 1 FROM pipeline_jobs j WHERE j.job_id = c.job_id)
               AND EXISTS (
                   SELECT 1 FROM pipeline_jobs_archive a
                    WHERE a.job_id = c.job_id
                      AND a.status IN ('done', 'error', 'cancelled', 'rejected', 'rejected_done')
               )
           );
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;
