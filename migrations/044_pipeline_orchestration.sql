-- 044_pipeline_orchestration.sql
-- AADS-211: 러너 오케스트레이션 — worker_model, parallel_group, depends_on

ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS worker_model VARCHAR(100);
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS parallel_group VARCHAR(100);
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS depends_on VARCHAR(100);

-- parallel_group 인덱스: 같은 그룹 내 작업 조회
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_parallel_group
  ON pipeline_jobs(parallel_group) WHERE parallel_group IS NOT NULL;

-- depends_on 인덱스: 의존성 체크
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_depends_on
  ON pipeline_jobs(depends_on) WHERE depends_on IS NOT NULL;

COMMENT ON COLUMN pipeline_jobs.worker_model IS '직접 지정 모델 — size 기반 자동선택 오버라이드';
COMMENT ON COLUMN pipeline_jobs.parallel_group IS '병렬 실행 그룹 — 같은 그룹 내 작업은 프로젝트 락 무시하고 동시 실행';
COMMENT ON COLUMN pipeline_jobs.depends_on IS '의존 작업 job_id — 해당 작업 done 후에만 ���행';
