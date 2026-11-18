-- Executed inside the migration and ledger transaction.
DO $$
BEGIN
    IF to_regclass('go100_deploy_approval_snapshots') IS NULL
       OR to_regclass('go100_deploy_approval_tenant_issued') IS NULL
       OR to_regprocedure('go100_deploy_approval_immutable()') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'go100_deploy_approval_snapshots'::regclass
                      AND tgname = 'go100_deploy_approval_immutable' AND NOT tgisinternal
                      AND tgenabled = 'O' AND tgtype = 58) THEN
        RAISE EXCEPTION 'GO100 approval migration verification failed';
    END IF;
END;
$$;
