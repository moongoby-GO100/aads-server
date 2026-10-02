-- AADS-DOC-CANONICAL-TOOL-PRECOMMIT-WARN-R2-20261003 — 정본 게이트 4번째 진입점(chat_tool) 허용.
-- 적용은 머지 후 별도 승인 단계에서 한다. 미적용이어도 게이트 기록은 fail-open 으로 건너뛴다.
-- 롤백: migrations/rollback/20261003_canonical_gate_events_chat_tool.down.sql
BEGIN;

ALTER TABLE canonical_gate_events DROP CONSTRAINT IF EXISTS canonical_gate_events_entrypoint_check;
ALTER TABLE canonical_gate_events ADD CONSTRAINT canonical_gate_events_entrypoint_check
    CHECK (entrypoint IN ('goal_api', 'commit', 'runner_submit', 'chat_tool'));

COMMIT;
