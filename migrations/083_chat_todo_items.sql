CREATE TABLE IF NOT EXISTS chat_todo_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    message_id UUID NULL REFERENCES chat_messages(id) ON DELETE SET NULL,
    execution_id UUID NULL REFERENCES chat_turn_executions(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    sort_order INTEGER NOT NULL DEFAULT 0,
    source VARCHAR(50) NOT NULL DEFAULT 'user_turn',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL,
    CONSTRAINT chat_todo_items_status_check
        CHECK (status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_chat_todo_items_session_status_sort
ON chat_todo_items(session_id, status, sort_order, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_todo_items_execution
ON chat_todo_items(execution_id, sort_order)
WHERE execution_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_chat_todo_items_message
ON chat_todo_items(message_id, sort_order)
WHERE message_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_todo_items_turn_order
ON chat_todo_items(session_id, execution_id, source, sort_order)
WHERE execution_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_todo_items_message_order
ON chat_todo_items(session_id, message_id, source, sort_order)
WHERE message_id IS NOT NULL;
