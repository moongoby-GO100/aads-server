-- M10 rollback removes enforcement only. Observation data is retained.
BEGIN;
DROP INDEX IF EXISTS idx_browser_live_facts_m10_freshness;
DROP POLICY IF EXISTS tenant_isolation ON browser_live_fact_events;
DROP POLICY IF EXISTS tenant_isolation ON browser_live_facts;
ALTER TABLE browser_live_fact_events DISABLE ROW LEVEL SECURITY;
ALTER TABLE browser_live_facts DISABLE ROW LEVEL SECURITY;
ALTER TABLE browser_live_fact_events
    DROP CONSTRAINT IF EXISTS fk_browser_live_fact_events_tenant_fact;
ALTER TABLE browser_live_facts
    DROP CONSTRAINT IF EXISTS fk_browser_live_facts_tenant_site,
    DROP CONSTRAINT IF EXISTS ck_browser_live_facts_m10_reason,
    DROP CONSTRAINT IF EXISTS ck_browser_live_facts_m10_fetched,
    DROP CONSTRAINT IF EXISTS ck_browser_live_facts_m10_type;
DROP INDEX IF EXISTS uq_browser_live_facts_tenant_id;
DROP INDEX IF EXISTS uq_authenticated_site_profiles_tenant_id;
COMMIT;
