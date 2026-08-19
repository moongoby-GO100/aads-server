-- AADS-P2-IS-HIDDEN-COLUMN: visible chat timeline filter hardening.
-- Non-destructive and idempotent. The trigger keeps future system/runner
-- messages hidden even when they are inserted outside chat_service._save_message.

ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN NOT NULL DEFAULT FALSE;

CREATE OR REPLACE FUNCTION set_chat_message_is_hidden()
RETURNS trigger AS $$
BEGIN
    NEW.is_hidden :=
        COALESCE(NEW.intent, '') IN (
            'system_trigger',
            'pipeline_c_start',
            'pipeline_c_result',
            'pipeline_c',
            'auto_reaction',
            'streaming_placeholder',
            'interrupted_partial',
            'interruption_notice',
            '_archived_partial',
            'runner_response',
            'pipeline_runner',
            'runner_notification',
            'ai_review_warning',
            '_deleted_duplicate'
        )
        OR (NEW.role = 'assistant' AND COALESCE(NEW.content, '') LIKE '%[Pipeline Runner]%')
        OR (NEW.role = 'assistant' AND COALESCE(NEW.content, '') LIKE '%[Runner]%')
        OR (NEW.role = 'user' AND COALESCE(NEW.content, '') LIKE '[시스템]%');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chat_messages_is_hidden ON chat_messages;

CREATE TRIGGER trg_chat_messages_is_hidden
BEFORE INSERT OR UPDATE OF role, content, intent
ON chat_messages
FOR EACH ROW
EXECUTE FUNCTION set_chat_message_is_hidden();

UPDATE chat_messages
SET is_hidden =
    COALESCE(intent, '') IN (
        'system_trigger',
        'pipeline_c_start',
        'pipeline_c_result',
        'pipeline_c',
        'auto_reaction',
        'streaming_placeholder',
        'interrupted_partial',
        'interruption_notice',
        '_archived_partial',
        'runner_response',
        'pipeline_runner',
        'runner_notification',
        'ai_review_warning',
        '_deleted_duplicate'
    )
    OR (role = 'assistant' AND COALESCE(content, '') LIKE '%[Pipeline Runner]%')
    OR (role = 'assistant' AND COALESCE(content, '') LIKE '%[Runner]%')
    OR (role = 'user' AND COALESCE(content, '') LIKE '[시스템]%');

CREATE INDEX IF NOT EXISTS idx_chat_messages_visible_session_created
    ON chat_messages(session_id, created_at)
    WHERE is_hidden = FALSE;

CREATE INDEX IF NOT EXISTS idx_chat_messages_visible_tenant_session_created
    ON chat_messages(tenant_id, session_id, created_at)
    WHERE is_hidden = FALSE;
