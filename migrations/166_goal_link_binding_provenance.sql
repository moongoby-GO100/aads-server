-- 166_goal_link_binding_provenance.sql
-- Goal Control P0 무결성 복구: goal_task_links 에 바인딩 출처 / 링크 생명주기 /
-- 재시도 승계(supersession) / 재조정 감사 컬럼을 추가한다.
--
-- 배경 (2026-09-09 프로덕션 확인):
--  * _auto_link_job_to_goal 이 project 만 보고 첫 활성 목표에 붙여서 무관한
--    AADS 작업(OHVIS trace receiver 등)이 "채팅 시스템 안정화" 목표에 묶였다.
--  * pipeline_jobs 가 terminated/error/cancelled 로 끝나도 링크는 queued 로 남았다.
--  * 과거 실패 링크를 일괄 failed 로 바꾸면 재시도로 이미 성공한 마일스톤이
--    영구히 blocked 가 되므로, 승계 근거를 저장할 자리가 필요하다.
--
-- 모든 문장은 IF NOT EXISTS / 조건부라 반복 실행해도 안전하다.
-- 행을 삭제하지 않는다 — 분리는 link_state='detached' 로만 표현한다.

-- ─── 1) pipeline_jobs: 명시적 목표 컨텍스트 보존 ───────────────────────────
-- 제출 시점에 호출자가 지정한 목표/마일스톤을 작업 행에 그대로 남긴다.
-- FK 를 걸지 않는다: pipeline_jobs 는 대용량 이력 테이블이고, 목표가 지워져도
-- 작업 이력은 남아야 하며, 프로젝트/활성 검증은 제출 API 에서 수행한다.
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS goal_id UUID;
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS milestone_id UUID;

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_goal
    ON pipeline_jobs(goal_id) WHERE goal_id IS NOT NULL;

-- ─── 2) goal_task_links: 계보 + 생명주기 + 승계 + 재조정 감사 ──────────────
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS bind_source TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS bound_by TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS link_state TEXT NOT NULL DEFAULT 'active';
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS detach_reason TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS superseded_by TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS last_job_status TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- link_state 어휘 고정: active(정상) | detached(오연결 분리) | orphan(작업 행 소실)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'goal_task_links_link_state_chk'
    ) THEN
        ALTER TABLE goal_task_links
            ADD CONSTRAINT goal_task_links_link_state_chk
            CHECK (link_state IN ('active', 'detached', 'orphan'));
    END IF;
END $$;

-- 기존 행은 전부 "프로젝트만 보고 붙인 레거시 자동연결"로 표시한다.
-- 데이터는 바꾸지 않고 출처만 기록하므로 재조정기가 안전/위험 링크를 구분할 수 있다.
UPDATE goal_task_links
SET bind_source = 'legacy_auto_project'
WHERE bind_source IS NULL;

CREATE INDEX IF NOT EXISTS idx_goal_task_links_state
    ON goal_task_links(link_state, task_type, task_id);
CREATE INDEX IF NOT EXISTS idx_goal_task_links_open
    ON goal_task_links(milestone_id)
    WHERE link_state = 'active' AND superseded_by IS NULL;

COMMENT ON COLUMN goal_task_links.bind_source IS
    'explicit_api | explicit_directive | reconciler | legacy_auto_project';
COMMENT ON COLUMN goal_task_links.link_state IS
    'active | detached(오연결 분리, 행 보존) | orphan(pipeline_jobs 행 없음)';
COMMENT ON COLUMN goal_task_links.superseded_by IS
    '동일 instruction_hash 로 나중에 성공한 재시도 작업의 task_id — 실패 링크의 차단을 해제한다';
