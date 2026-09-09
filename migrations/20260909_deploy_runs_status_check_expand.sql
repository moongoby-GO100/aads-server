-- Migration: expand deploy_runs status CHECK constraint
-- Date: 2026-09-09
-- Reason: deploy_observe_update and heartbeat write status='syncing_standby'/'verifying'/'awaiting_approval'
--         but the CHECK constraint only allowed 8 values. Silent CHECK violations caused heartbeat
--         starvation during standby_same_digest_sync, leading to false stale_auto reconciliation.
-- Applied live: 2026-09-09 21:51 KST (before deploy #279 completion)

BEGIN;

ALTER TABLE deploy_runs DROP CONSTRAINT IF EXISTS deploy_runs_status_check;

ALTER TABLE deploy_runs ADD CONSTRAINT deploy_runs_status_check
    CHECK (status = ANY (ARRAY[
        'running', 'success', 'failed', 'blocked',
        'superseded', 'cancelled', 'queued', 'pending',
        'syncing_standby', 'verifying', 'awaiting_approval'
    ]));

COMMIT;
