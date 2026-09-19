-- G6 reproducible golden/regression promotion gate for every learned artifact.
-- Included in the M7-M11 release chain so candidate-only learning always has
-- its promotion lifecycle installed before the site-learning extension.
CREATE TABLE IF NOT EXISTS browser_learned_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL CHECK (artifact_type IN ('page_template','site_skill','recovery')),
    artifact_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, artifact_type, artifact_key)
);

CREATE TABLE IF NOT EXISTS browser_learned_artifact_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id UUID NOT NULL REFERENCES browser_learned_artifacts(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate','shadow','active','deprecated','quarantined')),
    payload JSONB NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_active_id UUID REFERENCES browser_learned_artifact_versions(id),
    quarantine_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    activated_at TIMESTAMPTZ,
    UNIQUE (artifact_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_artifact_one_active
    ON browser_learned_artifact_versions (artifact_id) WHERE status='active';

CREATE OR REPLACE FUNCTION enforce_browser_learned_version_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    expected_hash TEXT;
BEGIN
    expected_hash := 'sha256:' || encode(digest(convert_to(NEW.payload::text, 'UTF8'), 'sha256'), 'hex');
    IF NEW.payload_sha256 IS DISTINCT FROM expected_hash THEN
        RAISE EXCEPTION 'learned artifact payload hash mismatch';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'candidate' OR NEW.previous_active_id IS NOT NULL THEN
            RAISE EXCEPTION 'learned artifact versions must enter as candidate';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256 THEN
        RAISE EXCEPTION 'learned artifact version payload is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
       AND (OLD.status, NEW.status) NOT IN (
           ('candidate','shadow'), ('candidate','quarantined'),
           ('shadow','active'), ('shadow','quarantined'),
           ('active','deprecated'), ('deprecated','active')
       ) THEN
        RAISE EXCEPTION 'learned artifact lifecycle bypass';
    END IF;
    IF NEW.status = 'active' AND NEW.activated_at IS NULL THEN
        RAISE EXCEPTION 'active learned artifact requires activation evidence';
    END IF;
    IF NEW.status = 'quarantined' AND COALESCE(NEW.quarantine_reason, '') = '' THEN
        RAISE EXCEPTION 'quarantined learned artifact requires reason';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_trigger
         WHERE tgname = 'trg_browser_learned_version_lifecycle'
           AND tgrelid = 'browser_learned_artifact_versions'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_browser_learned_version_lifecycle
        BEFORE INSERT OR UPDATE ON browser_learned_artifact_versions
        FOR EACH ROW EXECUTE FUNCTION enforce_browser_learned_version_lifecycle();
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS browser_promotion_ledgers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    artifact_id UUID NOT NULL,
    version_id UUID NOT NULL,
    idempotency_key TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('promoted','blocked','rolled_back')),
    reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    focused_results JSONB NOT NULL DEFAULT '{}'::jsonb,
    affected_regressions JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    active_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, idempotency_key)
);

-- Skill versions use the same gate while retaining their existing canonical tables.
ALTER TABLE ops_skill_versions
    ADD COLUMN IF NOT EXISTS previous_active_id UUID REFERENCES ops_skill_versions(id),
    ADD COLUMN IF NOT EXISTS quarantine_reason TEXT;
DO $$
DECLARE
    existing_definition TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid)
      INTO existing_definition
      FROM pg_constraint
     WHERE conrelid = 'ops_skill_versions'::regclass
       AND conname = 'ops_skill_versions_status_check';

    IF existing_definition IS NULL THEN
        ALTER TABLE ops_skill_versions
            ADD CONSTRAINT ops_skill_versions_status_check
            CHECK (status IN ('draft','candidate','shadow','active','deprecated','quarantined'));
    ELSIF existing_definition NOT ILIKE '%quarantined%'
       OR existing_definition NOT ILIKE '%deprecated%'
       OR existing_definition NOT ILIKE '%shadow%' THEN
        RAISE EXCEPTION 'incompatible ops_skill_versions_status_check; additive migration refused';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION prevent_ops_skill_version_contract_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.manifest->>'status' IS DISTINCT FROM NEW.status
       OR NEW.content::jsonb IS DISTINCT FROM NEW.manifest
       OR NEW.content_sha256 IS DISTINCT FROM
          ('sha256:' || encode(digest(convert_to(NEW.content, 'UTF8'), 'sha256'), 'hex')) THEN
        RAISE EXCEPTION 'skill version persisted contract mismatch';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'candidate' THEN
            RAISE EXCEPTION 'skill versions must enter as candidate';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF (OLD.status, NEW.status) NOT IN (
            ('candidate','shadow'), ('candidate','quarantined'),
            ('shadow','active'), ('shadow','quarantined'),
            ('active','deprecated'), ('deprecated','active')
        ) THEN
            RAISE EXCEPTION 'skill version lifecycle bypass';
        END IF;
    ELSIF NEW.skill_id IS DISTINCT FROM OLD.skill_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
       OR NEW.content IS DISTINCT FROM OLD.content
       OR NEW.manifest IS DISTINCT FROM OLD.manifest THEN
        RAISE EXCEPTION 'skill version contract is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_trigger
         WHERE tgname = 'trg_ops_skill_version_immutable'
           AND tgrelid = 'ops_skill_versions'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_ops_skill_version_immutable
        BEFORE INSERT OR UPDATE ON ops_skill_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_ops_skill_version_contract_mutation();
    END IF;
END
$$;
