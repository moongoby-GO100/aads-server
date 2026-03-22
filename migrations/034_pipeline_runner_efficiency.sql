-- Migration 034: Pipeline Runner 효율성 개선
-- Date: 2026-03-22
-- Goal: 성공률 38% → 70% — 에러 로깅 강화, stale 감지 개선

-- 1) error_detail 컬럼 추가 — 에러 원인 분류 (timeout, claude_code_crash, git_conflict, build_fail, etc.)
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS error_detail TEXT;

-- 2) runner_pid 컬럼 — Claude Code 프로세스 생존 확인용
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS runner_pid INTEGER;

-- 3) started_at 컬럼 — 실제 실행 시작 시간 (최대 실행 시간 타임아웃 판단용)
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;

-- 4) instruction_hash 컬럼 — 중복 작업 감지용
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS instruction_hash VARCHAR(64);

-- 5) 인덱스: error 상태 + error_detail 조합 조회 최적화
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_error_detail ON pipeline_jobs(status, error_detail) WHERE status = 'error';

-- 6) 인덱스: 중복 감지용 (project + instruction_hash + status)
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_dedup ON pipeline_jobs(project, instruction_hash, status) WHERE instruction_hash IS NOT NULL;

-- 7) 인덱스: running 상태 started_at (stale 감지용)
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_running_started ON pipeline_jobs(started_at) WHERE status = 'running';
