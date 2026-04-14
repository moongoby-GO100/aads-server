-- AADS-WRAP-GATE: pipeline_jobs에 wrap_file 컬럼 추가
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS wrap_file TEXT;
