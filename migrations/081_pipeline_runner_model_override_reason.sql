-- AADS-245: require and persist a reason for direct runner model overrides.

ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS model_override_reason TEXT;

COMMENT ON COLUMN pipeline_jobs.model_override_reason IS
    '직접 worker_model 지정 사유. 비어 있으면 어드민 runner_model_config 기반 자동 선택을 사용한다.';
