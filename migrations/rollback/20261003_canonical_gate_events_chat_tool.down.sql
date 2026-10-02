BEGIN;

DELETE FROM canonical_gate_events WHERE entrypoint = 'chat_tool';
ALTER TABLE canonical_gate_events DROP CONSTRAINT IF EXISTS canonical_gate_events_entrypoint_check;
ALTER TABLE canonical_gate_events ADD CONSTRAINT canonical_gate_events_entrypoint_check
    CHECK (entrypoint IN ('goal_api', 'commit', 'runner_submit'));

COMMIT;
