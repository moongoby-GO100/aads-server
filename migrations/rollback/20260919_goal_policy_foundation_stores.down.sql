-- W-12b/W-12c non-destructive rollback runbook.
-- The new ledgers and tenant fences intentionally have no automated DOWN that
-- drops data or weakens isolation. Roll back application readers/writers first;
-- retain all tables, RLS policies, append-only triggers, and audit records.
-- A later approved retirement migration may revoke writers and archive records.
BEGIN;
COMMENT ON TABLE goal_policy_decisions IS
    'W-12b retained during rollback: canonical append-only policy decision ledger';
COMMENT ON TABLE goal_auto_approval_use_events IS
    'W-12b retained during rollback: append-only use event ledger';
COMMENT ON TABLE work_item_review_decisions IS
    'W-12b retained during rollback: append-only independent review ledger';
COMMIT;
