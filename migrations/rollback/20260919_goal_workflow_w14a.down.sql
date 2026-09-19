-- W-14a non-destructive rollback runbook.
-- Roll application writers back first. Approval/effect audit records and
-- immutable request hashes are retained; dropping them would destroy evidence.
BEGIN;
COMMENT ON TABLE work_item_change_set_approval_decisions IS
    'W-14a retained during rollback: append-only independent approval evidence';
COMMENT ON TABLE goal_workflow_effects IS
    'W-14a retained during rollback: execution-key exactly-once effect evidence';
COMMENT ON COLUMN work_item_change_sets.body_hash IS
    'W-14a retained during rollback: immutable idempotency request fingerprint';
COMMIT;
