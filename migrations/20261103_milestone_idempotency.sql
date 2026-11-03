-- Apply before enabling keyed API calls; old applications ignore this additive table.
-- Revert application first. Keep this table across rollback to preserve replay identity.
-- Dropping it loses keys and permits duplicate creation on subsequent retries.
BEGIN;
CREATE UNIQUE INDEX IF NOT EXISTS uq_milestone_idempotency_goal_tenant
    ON goals (id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_milestone_idempotency_record_scope
    ON milestones (id, goal_id, tenant_id);
CREATE TABLE IF NOT EXISTS milestone_idempotency (
    tenant_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    idempotency_key varchar(200) NOT NULL CHECK (length(btrim(idempotency_key)) > 0),
    payload_fingerprint varchar(64) NOT NULL CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
    milestone_id uuid NOT NULL,
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'object'
        AND response ? 'milestone_id' AND response->>'milestone_id' = milestone_id::text),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, goal_id, idempotency_key),
    UNIQUE (milestone_id),
    FOREIGN KEY (goal_id, tenant_id) REFERENCES goals (id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (milestone_id, goal_id, tenant_id)
        REFERENCES milestones (id, goal_id, tenant_id) ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED
);
COMMIT;
