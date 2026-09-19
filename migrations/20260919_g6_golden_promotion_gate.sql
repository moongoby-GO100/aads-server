-- G6 reproducible golden/regression promotion gate for every learned artifact.
CREATE TABLE IF NOT EXISTS browser_learned_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
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

CREATE TABLE IF NOT EXISTS browser_promotion_ledgers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
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
ALTER TABLE ops_skill_versions DROP CONSTRAINT IF EXISTS ops_skill_versions_status_check;
ALTER TABLE ops_skill_versions ADD CONSTRAINT ops_skill_versions_status_check
    CHECK (status IN ('draft','candidate','shadow','active','deprecated','quarantined'));

CREATE OR REPLACE FUNCTION prevent_ops_skill_version_contract_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.manifest->>'status' IS DISTINCT FROM NEW.status
       OR NEW.content::jsonb IS DISTINCT FROM NEW.manifest
       OR NEW.content_sha256 IS DISTINCT FROM
          ('sha256:' || encode(digest(convert_to(NEW.content, 'UTF8'), 'sha256'), 'hex')) THEN
        RAISE EXCEPTION 'skill version persisted contract mismatch';
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
