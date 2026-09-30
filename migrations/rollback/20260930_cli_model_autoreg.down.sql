-- Rollback of migrations/20260930_cli_model_autoreg.sql.
-- 확장 상태 행은 지우지 않고 'candidate' 로 되돌린다(원래 상태는 notes 앞에 남김) — 그래야 원 CHECK 를 다시 걸 수 있다.
BEGIN;

UPDATE llm_model_candidates
SET notes = '[rollback cli_autoreg status=' || status || '] ' || COALESCE(notes, ''),
    status = 'candidate'
WHERE status IN ('discovered','verified','blocked_account','probe_failed');

ALTER TABLE llm_model_candidates DROP CONSTRAINT IF EXISTS ck_llm_model_candidates_status;
ALTER TABLE llm_model_candidates ADD CONSTRAINT ck_llm_model_candidates_status
    CHECK (status IN ('candidate','testing','approved','rejected','retired'));

DROP INDEX IF EXISTS idx_llm_model_candidates_probe;
ALTER TABLE llm_model_candidates DROP COLUMN IF EXISTS last_probe_at;
ALTER TABLE llm_model_candidates DROP COLUMN IF EXISTS discovery_source;

COMMIT;
