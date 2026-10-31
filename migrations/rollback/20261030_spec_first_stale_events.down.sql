-- 새 경고 감사 행 제거 후 추가된 CHECK 분기만 되돌린다.
BEGIN;
DELETE FROM canonical_gate_events
WHERE entrypoint IN ('chat_spec_first', 'runner_approve_stale');
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT conname, pg_get_expr(conbin, conrelid) AS expression
        FROM pg_constraint
        WHERE conrelid = 'canonical_gate_events'::regclass
          AND conname IN ('canonical_gate_events_entrypoint_check', 'canonical_gate_events_verdict_check')
    LOOP
        EXECUTE format('ALTER TABLE canonical_gate_events DROP CONSTRAINT %I', item.conname);
        EXECUTE format('ALTER TABLE canonical_gate_events ADD CONSTRAINT %I CHECK ((%s) AND %s)',
            item.conname, item.expression,
            CASE item.conname WHEN 'canonical_gate_events_entrypoint_check'
                THEN 'entrypoint NOT IN (''chat_spec_first'', ''runner_approve_stale'')'
                ELSE 'verdict NOT IN (''spec_first'', ''stale_spec'')' END);
    END LOOP;
END $$;
COMMIT;
