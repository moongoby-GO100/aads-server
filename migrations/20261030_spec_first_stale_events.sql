-- 문서 우선·낡은 판본 경고 감사값 추가. 기존 CHECK 허용값은 보존한다.
BEGIN;
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT conname, pg_get_expr(conbin, conrelid) AS expression,
               CASE conname
                   WHEN 'canonical_gate_events_entrypoint_check' THEN
                       'entrypoint IN (''chat_spec_first'', ''runner_approve_stale'')'
                   ELSE 'verdict IN (''spec_first'', ''stale_spec'')'
               END AS extra
        FROM pg_constraint
        WHERE conrelid = 'canonical_gate_events'::regclass
          AND conname IN ('canonical_gate_events_entrypoint_check', 'canonical_gate_events_verdict_check')
    LOOP
        EXECUTE format('ALTER TABLE canonical_gate_events DROP CONSTRAINT %I', item.conname);
        EXECUTE format('ALTER TABLE canonical_gate_events ADD CONSTRAINT %I CHECK ((%s) OR (%s))',
                       item.conname, item.expression, item.extra);
    END LOOP;
END $$;
COMMIT;
