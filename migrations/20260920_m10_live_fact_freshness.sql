-- M10 live-data completion: additive, repeatable freshness/provenance fencing.
BEGIN;

ALTER TABLE browser_live_facts
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reason_code TEXT;
ALTER TABLE browser_live_fact_events
    ADD COLUMN IF NOT EXISTS reason_code TEXT;

UPDATE browser_live_facts SET fetched_at = observed_at WHERE fetched_at IS NULL;
ALTER TABLE browser_live_facts ALTER COLUMN fetched_at SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_authenticated_site_profiles_tenant_id
    ON authenticated_site_profiles(tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_live_facts_tenant_id
    ON browser_live_facts(tenant_id, id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_browser_live_facts_m10_type') THEN
        ALTER TABLE browser_live_facts ADD CONSTRAINT ck_browser_live_facts_m10_type
            CHECK (fact_type IN ('price','stock','inventory','shipping','delivery','seller')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_browser_live_facts_m10_fetched') THEN
        ALTER TABLE browser_live_facts ADD CONSTRAINT ck_browser_live_facts_m10_fetched
            CHECK (fetched_at >= observed_at AND expires_at > fetched_at) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_browser_live_facts_m10_reason') THEN
        ALTER TABLE browser_live_facts ADD CONSTRAINT ck_browser_live_facts_m10_reason
            CHECK ((freshness_status = 'CURRENT' AND reason_code IS NULL)
                OR (freshness_status <> 'CURRENT' AND btrim(reason_code) <> '')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_browser_live_facts_tenant_site') THEN
        ALTER TABLE browser_live_facts ADD CONSTRAINT fk_browser_live_facts_tenant_site
            FOREIGN KEY (tenant_id, site_profile_id)
            REFERENCES authenticated_site_profiles(tenant_id, id) ON DELETE RESTRICT NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_browser_live_fact_events_tenant_fact') THEN
        ALTER TABLE browser_live_fact_events ADD CONSTRAINT fk_browser_live_fact_events_tenant_fact
            FOREIGN KEY (tenant_id, fact_id)
            REFERENCES browser_live_facts(tenant_id, id) ON DELETE CASCADE NOT VALID;
    END IF;
END $$;

ALTER TABLE browser_live_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE browser_live_fact_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE browser_live_facts FORCE ROW LEVEL SECURITY;
ALTER TABLE browser_live_fact_events FORCE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='browser_live_facts' AND policyname='tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON browser_live_facts
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='browser_live_fact_events' AND policyname='tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON browser_live_fact_events
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_browser_live_facts_m10_freshness
    ON browser_live_facts(tenant_id, site_profile_id, fact_type, entity_key, expires_at, freshness_status);

COMMIT;
