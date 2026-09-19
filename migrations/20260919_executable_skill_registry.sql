-- G3 executable Site Skill registry. Additive extension of migration 158.
ALTER TABLE ops_skill_library
    ADD COLUMN IF NOT EXISTS tenant_id UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000'::uuid;

CREATE INDEX IF NOT EXISTS idx_ops_skill_library_tenant_slug
    ON ops_skill_library (tenant_id, slug);

-- Migration 158 used a global slug key. Keep that stricter legacy invariant:
-- production release assets are additive and never drop constraints.  The
-- tenant-scoped index below is the runtime lookup/idempotency contract.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_skill_library_tenant_slug_unique
    ON ops_skill_library (tenant_id, slug);

ALTER TABLE ops_skill_versions
    ADD COLUMN IF NOT EXISTS manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS promoted_by TEXT,
    ADD COLUMN IF NOT EXISTS promotion_evidence JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_skill_one_active
    ON ops_skill_versions (skill_id) WHERE status = 'active';

ALTER TABLE ops_skill_runs
    ADD COLUMN IF NOT EXISTS tenant_id UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000'::uuid,
    ADD COLUMN IF NOT EXISTS skill_version_id UUID REFERENCES ops_skill_versions(id),
    ADD COLUMN IF NOT EXISTS input_hash TEXT,
    ADD COLUMN IF NOT EXISTS result_hash TEXT,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
    ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS policy_decision JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS channel_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS approval_id UUID REFERENCES agent_permission_requests(id),
    ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12,6),
    ADD COLUMN IF NOT EXISTS latency_ms INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_skill_runs_tenant_idempotency
    ON ops_skill_runs (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ops_skill_versions_skill_status
    ON ops_skill_versions (skill_id, status, created_at DESC);

-- Published versions are immutable, except for the explicit active -> retired
-- lifecycle transition.  The transition updates the embedded lifecycle status
-- and its hash atomically, so the three persisted contract representations
-- never disagree.
CREATE OR REPLACE FUNCTION prevent_ops_skill_version_contract_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.manifest->>'status' IS DISTINCT FROM NEW.status THEN
        RAISE EXCEPTION 'skill version manifest status must match row status';
    END IF;
    IF NEW.content::jsonb IS DISTINCT FROM NEW.manifest THEN
        RAISE EXCEPTION 'skill version content must match manifest';
    END IF;
    IF NEW.content_sha256 IS DISTINCT FROM
       ('sha256:' || encode(digest(convert_to(NEW.content, 'UTF8'), 'sha256'), 'hex')) THEN
        RAISE EXCEPTION 'skill version content_sha256 must match content';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status
       OR NEW.skill_id IS DISTINCT FROM OLD.skill_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
       OR NEW.content IS DISTINCT FROM OLD.content
       OR NEW.manifest IS DISTINCT FROM OLD.manifest THEN
        IF (OLD.status, NEW.status) IN (('candidate', 'active'), ('shadow', 'active'), ('active', 'retired'))
           AND NEW.skill_id IS NOT DISTINCT FROM OLD.skill_id
           AND NEW.version IS NOT DISTINCT FROM OLD.version
           AND NEW.manifest = jsonb_set(OLD.manifest, '{status}', to_jsonb(NEW.status)) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'skill version contract is immutable outside promotion lifecycle';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_ops_skill_version_immutable'
          AND tgrelid = 'ops_skill_versions'::regclass
    ) THEN
        CREATE TRIGGER trg_ops_skill_version_immutable
        BEFORE UPDATE ON ops_skill_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_ops_skill_version_contract_mutation();
    END IF;
END;
$$;
