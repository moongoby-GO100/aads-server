-- Defense in depth for the diagnostics-only PC Agent disconnect policy.
-- Existing messages and diagnostic observations are intentionally preserved.

CREATE OR REPLACE FUNCTION block_pc_agent_disconnect_chat_message()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.intent = 'pc_agent_alert' THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_block_pc_agent_disconnect_chat_message ON chat_messages;

CREATE TRIGGER trg_block_pc_agent_disconnect_chat_message
BEFORE INSERT ON chat_messages
FOR EACH ROW
EXECUTE FUNCTION block_pc_agent_disconnect_chat_message();

COMMENT ON FUNCTION block_pc_agent_disconnect_chat_message() IS
    'Blocks PC Agent disconnect alerts from CEO chat while preserving diagnostics.';

-- Rollback (only if chat delivery is explicitly restored):
-- DROP TRIGGER IF EXISTS trg_block_pc_agent_disconnect_chat_message ON chat_messages;
-- DROP FUNCTION IF EXISTS block_pc_agent_disconnect_chat_message();
