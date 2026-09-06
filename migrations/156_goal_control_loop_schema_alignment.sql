-- 156_goal_control_loop_schema_alignment.sql
-- Phase 2-1: 목표 Control Loop 스키마 보강
-- 153_goal_management.sql이 이미 goals/milestones/goal_task_links를 생성했으나
-- (1) 완료 판정 기준 필드가 없고 (2) app/services/temporal_controller.py가 참조하는
-- due_date/started_at 컬럼이 없으며 (3) goal_task_links가 milestone 경유 없이는
-- 목표에 직접 연결할 수 없어 이를 보강한다.

ALTER TABLE goals ADD COLUMN IF NOT EXISTS success_criteria TEXT;

ALTER TABLE milestones ADD COLUMN IF NOT EXISTS completion_criteria TEXT;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS due_date TIMESTAMPTZ;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;

-- goal_id 경유 직접 연결 지원: milestone_id는 nullable로 완화
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS goal_id UUID REFERENCES goals(id) ON DELETE CASCADE;
ALTER TABLE goal_task_links ALTER COLUMN milestone_id DROP NOT NULL;
ALTER TABLE goal_task_links ADD CONSTRAINT goal_task_links_goal_or_milestone_chk
    CHECK (goal_id IS NOT NULL OR milestone_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_goal_task_links_goal ON goal_task_links(goal_id);
CREATE INDEX IF NOT EXISTS idx_milestones_due_date ON milestones(due_date) WHERE due_date IS NOT NULL;
