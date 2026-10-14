-- R-DOC M8a: persisted verdicts of the mockup approval gate (all phases), so the shadow period leaves evidence.
-- Additive and idempotent. Not applied to production here: it is applied at deploy approval.
-- Plain uuid/text columns (no FK) on purpose: a verdict must be writable even when the review, job or tenant
-- row is gone, and this table is evidence, not part of the approval state machine.
-- Rolled back by migrations/rollback/20261014_mockup_gate_decisions.down.sql.
BEGIN;

CREATE TABLE IF NOT EXISTS mockup_gate_decisions (
    id bigserial PRIMARY KEY,
    decided_at timestamptz NOT NULL DEFAULT now(),
    decided_at_kst timestamp GENERATED ALWAYS AS (decided_at AT TIME ZONE 'Asia/Seoul') STORED,
    phase text NOT NULL CHECK (phase IN ('submit','pre_execution','checkpoint','chat_write','pre_push')),
    mode text NOT NULL CHECK (mode IN ('off','shadow','enforce')),
    allowed boolean NOT NULL,
    blocking boolean NOT NULL,
    shadow boolean NOT NULL DEFAULT false,
    reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    ui_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    bundle_declared boolean NOT NULL DEFAULT false,
    task_id text CHECK (task_id IS NULL OR length(task_id) BETWEEN 1 AND 200),
    job_id text,
    project_key text,
    tenant_id uuid,
    review_id uuid,
    revision_id uuid,
    audit_event_id bigint,
    actor text
);

CREATE INDEX IF NOT EXISTS mockup_gate_decisions_time_idx ON mockup_gate_decisions(decided_at DESC);
CREATE INDEX IF NOT EXISTS mockup_gate_decisions_verdict_idx ON mockup_gate_decisions(mode, allowed, decided_at DESC);
CREATE INDEX IF NOT EXISTS mockup_gate_decisions_scope_idx
    ON mockup_gate_decisions(tenant_id, project_key, phase, decided_at DESC);

COMMIT;
