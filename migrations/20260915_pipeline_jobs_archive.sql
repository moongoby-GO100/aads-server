-- 20260915_pipeline_jobs_archive.sql
--
-- 왜 만드는가 (2026-09-15 실측):
--   AADS-191 의 pipeline_cleanup 은 done/error/cancelled/rejected 를 1시간 뒤 DELETE 한다.
--   그 결과 성공한 작업은 흔적 없이 사라지고, DELETE 절에 빠진 'rejected_done' 만
--   2026-04-02 부터 710건 무한 누적됐다. ACCT 는 pipeline_runner_events 에 job_started 가
--   9건 있는데 pipeline_jobs 에는 전 기간 0건이다 — 프로젝트별 성공률·소요시간·비용을
--   pipeline_jobs 로 산출할 방법이 없다.
--
-- 무엇을 하는가:
--   삭제 대신 아카이브로 옮긴다. 큐 테이블은 그대로 작게 유지되고 이력은 남는다.
--   컬럼 드리프트를 피하려고 행 전체를 jsonb 로 넣는다(pipeline_jobs 는 46컬럼이고 계속 는다).
--   git_diff/logs/result_output 은 용량이 커서 빼둔다 — diff 는 git 에서 복원된다.

CREATE TABLE IF NOT EXISTS pipeline_jobs_archive (
    job_id      text PRIMARY KEY,
    project     text,
    status      text,
    runner_host text,
    created_at  timestamptz,
    updated_at  timestamptz,
    archived_at timestamptz NOT NULL DEFAULT now(),
    row_data    jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pja_project_created
    ON pipeline_jobs_archive (project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pja_archived_at
    ON pipeline_jobs_archive (archived_at);

COMMENT ON TABLE pipeline_jobs_archive IS
    'pipeline_jobs 종료 작업 이력 보관. pipeline_cleanup 이 삭제 대신 여기로 옮긴다 (2026-09-15).';
