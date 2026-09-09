-- 165_goal_link_binding_and_supersession.sql
-- Goal Control P0 무결성 복구 — 명시적 목표 바인딩 + 링크 계보(provenance)/승계 메타데이터.
--
-- 배경 (2026-09-09 운영 실측):
--   1) pipeline_runner_service._auto_link_job_to_goal 이 "프로젝트의 첫 active 목표"에만
--      의존해 무관한 작업(OHVIS trace 수신기 등)까지 채팅 안정화 목표에 붙었다.
--   2) pipeline_jobs 가 종료돼도 goal_task_links.status 가 queued/pending 으로 남았다.
--   3) 실패 링크를 무조건 failed 로 굳히면 재시도로 대체된 과거 실패가 마일스톤을
--      영구히 blocked 로 만든다 → 승계(supersession) 근거를 저장할 자리가 필요하다.
--
-- 이 마이그레이션은 **컬럼/인덱스만 추가**한다. 기존 행의 값을 바꾸거나 지우지 않는다.
-- 실제 재조정은 app/services/goal_link_reconciler.py 의 dry-run → 제한적 repair 로 수행한다.
-- 멱등: 전부 IF NOT EXISTS. 반복 실행해도 안전하다.

-- ─── 1) pipeline_jobs: 명시적 목표 컨텍스트 ────────────────────────────────
-- FK 를 걸지 않는다: pipeline_jobs 는 대용량 이력 테이블이고, 목표가 삭제돼도
-- 작업 이력은 남아야 하며, 검증은 제출 시점 애플리케이션에서 수행한다.
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS goal_id UUID;
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS milestone_id UUID;

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_goal
    ON pipeline_jobs(goal_id) WHERE goal_id IS NOT NULL;

-- ─── 2) goal_task_links: 계보 + 승계 + 재조정 감사 메타데이터 ───────────────
-- link_state: active(유효) | detached(오연결 회수) | orphan(작업 행 없음)
--   status 컬럼(작업 진행 상태)과 의미가 다르므로 분리한다.
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS link_state TEXT NOT NULL DEFAULT 'active';
-- bind_source: explicit_api | explicit_directive | reconciler | legacy_auto_project | unknown
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS bind_source TEXT NOT NULL DEFAULT 'unknown';
-- 승계: 같은 instruction_hash 의 후속 성공 작업이 과거 실패를 대체했다는 결정적 근거
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS superseded_by TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS supersede_reason TEXT;
-- 재조정 감사
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS reconcile_note TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_goal_task_links_state
    ON goal_task_links(link_state, status);
CREATE INDEX IF NOT EXISTS idx_goal_task_links_milestone_state
    ON goal_task_links(milestone_id, link_state) WHERE milestone_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_goal_task_links_superseded
    ON goal_task_links(superseded_by) WHERE superseded_by IS NOT NULL;
