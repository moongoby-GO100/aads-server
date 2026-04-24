CREATE TABLE IF NOT EXISTS chat_model_preferences (
    model_id VARCHAR(150) PRIMARY KEY,
    display_order INT NOT NULL DEFAULT 1000,
    is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
    is_favorite BOOLEAN NOT NULL DEFAULT FALSE,
    is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by VARCHAR(100) NOT NULL DEFAULT 'system',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_model_preferences_order
    ON chat_model_preferences(is_pinned DESC, is_favorite DESC, display_order ASC, model_id ASC);
