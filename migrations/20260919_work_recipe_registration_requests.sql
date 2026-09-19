-- WorkRecipe B-scope registration approval (FR-17).
-- Additive only: a recorded recipe remains a draft until an internal admin approves it.
BEGIN;

CREATE TABLE IF NOT EXISTS work_recipe_registration_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    domain          TEXT NOT NULL,
    spec_hash       TEXT NOT NULL,
    spec            JSONB NOT NULL,
    yaml_source     TEXT NOT NULL DEFAULT '',
    dry_run         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending',
    requested_by    TEXT NOT NULL DEFAULT '',
    decided_by      TEXT NOT NULL DEFAULT '',
    decision_reason TEXT NOT NULL DEFAULT '',
    recipe_id       UUID NULL REFERENCES work_recipes(id) ON DELETE SET NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT work_recipe_registration_status_valid
        CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_recipe_registration_pending
    ON work_recipe_registration_requests (tenant_id, domain, name, spec_hash)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_work_recipe_registration_pending
    ON work_recipe_registration_requests (tenant_id, requested_at DESC)
    WHERE status = 'pending';

COMMIT;
