-- WP04 online indexes.  This file MUST be executed as top-level statements.
-- CREATE INDEX CONCURRENTLY is illegal inside BEGIN/COMMIT transaction blocks.

SET lock_timeout = '5s';
SET statement_timeout = '30min';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_messages_wp04_keyset
    ON chat_messages (tenant_id, session_id, created_at DESC, id DESC)
    WHERE deleted_at IS NULL
      AND COALESCE(is_hidden, FALSE) = FALSE
      AND intent IS DISTINCT FROM '_deleted_duplicate';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_outbox_wp04_changes
    ON chat_outbox (tenant_id, session_id, session_revision, event_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_outbox_wp04_publish
    ON chat_outbox (created_at, event_id)
    WHERE published_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_execution_checkpoints_wp04_scope
    ON chat_execution_checkpoints (tenant_id, session_id, updated_at DESC);

RESET statement_timeout;
RESET lock_timeout;
