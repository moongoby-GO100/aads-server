-- M14 schema-only rollback. Business/audit rows must be exported before use.
BEGIN;
DROP TRIGGER IF EXISTS trg_goal_decision_log_append_only ON goal_approval_decision_logs;
DROP TRIGGER IF EXISTS trg_stale_policy_grants ON goal_approval_policy_versions;
DROP TRIGGER IF EXISTS trg_stale_assignment_grants ON project_role_assignments;
DROP FUNCTION IF EXISTS aads_decision_log_append_only();
DROP FUNCTION IF EXISTS aads_stale_policy_grants();
DROP FUNCTION IF EXISTS aads_stale_assignment_grants();
DROP TABLE IF EXISTS goal_approval_kill_switches;
DROP TABLE IF EXISTS goal_workflow_outbox;
DROP TABLE IF EXISTS goal_approval_decision_logs;
ALTER TABLE goal_auto_approval_grants DROP COLUMN IF EXISTS last_used_at;
ALTER TABLE work_item_change_sets DROP COLUMN IF EXISTS last_error;
ALTER TABLE work_item_change_sets DROP COLUMN IF EXISTS owner_epoch;
ALTER TABLE work_item_change_sets DROP COLUMN IF EXISTS owner_instance;
COMMIT;
