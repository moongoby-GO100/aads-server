-- 047: pipeline_jobs에 actual_model 컬럼 추가 (실제 실행된 모델 기록)
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS actual_model VARCHAR(100);
COMMENT ON COLUMN pipeline_jobs.actual_model IS '실제 실행된 모델명 (예: litellm:kimi-k2.5, claude:sonnet)';
