-- rollback: 20261022_directive_draft_turn_dedup.sql
-- 인덱스를 지우고, 이 마이그레이션이 표시한 행(superseded_reason 으로 식별)만 원래 status 로 되돌린다.
BEGIN;

DROP INDEX IF EXISTS ux_directive_drafts_selected_response_turn;

UPDATE directive_drafts
   SET status = COALESCE(classification->>'superseded_prev_status', status),
       classification = classification - 'superseded_by' - 'superseded_reason' - 'superseded_prev_status'
 WHERE classification->>'superseded_reason' = 'selected_response_turn_dedup';

COMMIT;
