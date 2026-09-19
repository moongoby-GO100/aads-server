-- W-14b follow-up: the append-only usage ledger is tenant-confined at DB level.
BEGIN;

ALTER TABLE goal_auto_approval_usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE goal_auto_approval_usage_events FORCE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname=current_schema()
           AND tablename='goal_auto_approval_usage_events'
           AND policyname='tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON goal_auto_approval_usage_events
            USING (tenant_id = aads_current_tenant_id())
            WITH CHECK (tenant_id = aads_current_tenant_id());
    END IF;
END $$;

COMMIT;
