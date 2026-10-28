-- 20261028 milestones.lead_triage_at — 담당 없는 막힘 마일스톤·업무를 주도에게 정리 요청한 시각.
-- 같은 마일스톤에 24시간에 한 번만 보내기 위한 기록이다. goal_dispatch.triage_unowned_milestones 가
-- 같은 ADD COLUMN IF NOT EXISTS 를 보장한다.
-- 되돌림: migrations/rollback/20261028_milestone_lead_triage.down.sql
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS lead_triage_at timestamptz;
