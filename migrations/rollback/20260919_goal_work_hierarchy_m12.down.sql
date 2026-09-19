-- Explicit M12 rollback for an empty/unreleased schema only.
-- Production rollback is feature-disable + additive schema retention per PRD 11.
BEGIN;
DROP TRIGGER IF EXISTS trg_milestone_scope ON milestones;
DROP TABLE IF EXISTS goal_auto_approval_uses;
DROP TABLE IF EXISTS goal_auto_approval_grants;
DROP TABLE IF EXISTS goal_approval_policy_versions;
DROP TABLE IF EXISTS work_item_events;
DROP TABLE IF EXISTS work_item_change_sets;
DROP TABLE IF EXISTS work_item_evidence;
DROP TABLE IF EXISTS work_item_dependencies;
DROP TABLE IF EXISTS work_items;
DROP TABLE IF EXISTS project_role_assignments;
DROP FUNCTION IF EXISTS aads_validate_work_item_dependency();
DROP FUNCTION IF EXISTS aads_validate_work_item_hierarchy();
DROP FUNCTION IF EXISTS aads_forbid_append_only_mutation();
DROP FUNCTION IF EXISTS aads_guard_change_set_immutability();
DROP FUNCTION IF EXISTS aads_guard_active_grant();
DROP FUNCTION IF EXISTS aads_set_milestone_scope();
ALTER TABLE milestones DROP CONSTRAINT IF EXISTS fk_milestones_goal_scope;
ALTER TABLE goals DROP CONSTRAINT IF EXISTS fk_goals_tenant;
DROP INDEX IF EXISTS ux_milestones_id_goal_tenant_project;
DROP INDEX IF EXISTS ux_goals_id_tenant_project;
ALTER TABLE milestones DROP COLUMN IF EXISTS project;
ALTER TABLE milestones DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE goals DROP COLUMN IF EXISTS tenant_id;
COMMIT;
