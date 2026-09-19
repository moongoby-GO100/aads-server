-- Read-only verification for W-12b/W-12c. Run after the additive migration.
DO $$
DECLARE
    expected_tables TEXT[] := ARRAY[
        'work_item_dependencies','work_item_evidence','goal_kill_switches','goal_policy_decisions',
        'goal_workflow_outbox','goal_execution_leases','goal_auto_approval_use_reservations',
        'goal_auto_approval_use_events','work_item_review_requirements','work_item_review_decisions'
    ];
    table_name TEXT;
    missing_columns TEXT[];
BEGIN
    FOREACH table_name IN ARRAY expected_tables LOOP
        IF to_regclass('public.' || table_name) IS NULL THEN
            RAISE EXCEPTION 'missing W-12b store: %', table_name;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_class
             WHERE oid=to_regclass('public.' || table_name)
               AND relrowsecurity AND relforcerowsecurity
        ) THEN RAISE EXCEPTION 'RLS is not forced: %', table_name; END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
             WHERE schemaname='public' AND tablename=table_name AND policyname='tenant_isolation'
        ) THEN RAISE EXCEPTION 'tenant policy is missing: %', table_name; END IF;
    END LOOP;

    SELECT array_agg(required.name ORDER BY required.name) INTO missing_columns
      FROM (VALUES
          ('goal_policy_decisions','decision_input_hash'),
          ('goal_policy_decisions','precondition_snapshot_hash'),
          ('goal_policy_decisions','canonicalization_version'),
          ('goal_policy_decisions','signature_key_id'),
          ('goal_policy_decisions','signature_key_version'),
          ('goal_policy_decisions','ancestor_revocation_epoch'),
          ('goal_workflow_outbox','payload_hash'),
          ('goal_workflow_outbox','sequence_no'),
          ('goal_execution_leases','owner_epoch'),
          ('goal_auto_approval_use_reservations','reconciliation_state'),
          ('work_item_review_decisions','override_reason')
      ) AS required(rel, name)
     WHERE NOT EXISTS (
         SELECT 1 FROM information_schema.columns c
          WHERE c.table_schema='public' AND c.table_name=required.rel AND c.column_name=required.name
     );
    IF missing_columns IS NOT NULL THEN
        RAISE EXCEPTION 'missing W-12b columns: %', missing_columns;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid='goal_policy_decisions'::regclass
                      AND tgname='trg_goal_policy_decisions_append_only' AND NOT tgisinternal)
       OR NOT EXISTS (SELECT 1 FROM pg_trigger
                      WHERE tgrelid='goal_auto_approval_use_events'::regclass
                        AND tgname='trg_goal_use_events_append_only' AND NOT tgisinternal)
       OR NOT EXISTS (SELECT 1 FROM pg_trigger
                      WHERE tgrelid='work_item_review_decisions'::regclass
                        AND tgname='trg_review_decisions_append_only' AND NOT tgisinternal) THEN
        RAISE EXCEPTION 'one or more append-only triggers are missing';
    END IF;
END $$;

DO $$ BEGIN
    IF to_regprocedure('goal_policy_execution_fences(uuid,uuid,uuid,uuid)') IS NULL THEN
        RAISE EXCEPTION 'goal_policy_execution_fences is missing';
    END IF;
END $$;
