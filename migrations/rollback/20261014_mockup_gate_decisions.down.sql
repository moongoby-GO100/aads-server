-- Rollback of 20261014_mockup_gate_decisions.sql: drops only the verdict evidence table (no approval state lives here).
BEGIN;
DROP TABLE IF EXISTS mockup_gate_decisions;
COMMIT;
