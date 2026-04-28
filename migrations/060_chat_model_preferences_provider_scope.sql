ALTER TABLE chat_model_preferences
    ADD COLUMN IF NOT EXISTS provider VARCHAR(80),
    ADD COLUMN IF NOT EXISTS preference_key VARCHAR(260);

UPDATE chat_model_preferences AS pref
SET provider = CASE
        WHEN pref.model_id IN ('mixture', 'auto') THEN 'auto'
        ELSE COALESCE(
            (
                SELECT MIN(models.provider)
                FROM llm_models AS models
                WHERE models.model_id = pref.model_id
                GROUP BY models.model_id
                HAVING COUNT(DISTINCT models.provider) = 1
            ),
            'legacy'
        )
    END
WHERE provider IS NULL OR provider = '';

UPDATE chat_model_preferences
SET preference_key = CASE
        WHEN model_id IN ('mixture', 'auto') THEN 'mixture'
        ELSE provider || ':' || model_id
    END
WHERE preference_key IS NULL OR preference_key = '';

ALTER TABLE chat_model_preferences
    ALTER COLUMN provider SET NOT NULL,
    ALTER COLUMN provider SET DEFAULT 'legacy',
    ALTER COLUMN preference_key SET NOT NULL;

DO $$
DECLARE
    pk_name TEXT;
BEGIN
    SELECT constraint_name INTO pk_name
    FROM information_schema.table_constraints
    WHERE table_schema = 'public'
      AND table_name = 'chat_model_preferences'
      AND constraint_type = 'PRIMARY KEY'
    LIMIT 1;

    IF pk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE chat_model_preferences DROP CONSTRAINT %I', pk_name);
    END IF;
END $$;

ALTER TABLE chat_model_preferences
    ADD CONSTRAINT chat_model_preferences_pkey PRIMARY KEY (preference_key);

CREATE INDEX IF NOT EXISTS idx_chat_model_preferences_model_provider
    ON chat_model_preferences(provider, model_id);

DROP INDEX IF EXISTS idx_chat_model_preferences_order;

CREATE INDEX IF NOT EXISTS idx_chat_model_preferences_order
    ON chat_model_preferences(is_pinned DESC, is_favorite DESC, display_order ASC, provider ASC, model_id ASC);

INSERT INTO chat_model_preferences (
    preference_key, provider, model_id, display_order, is_hidden, is_favorite, is_pinned, updated_by, updated_at
)
SELECT
    'codex:' || pref.model_id,
    'codex',
    pref.model_id,
    pref.display_order,
    pref.is_hidden,
    pref.is_favorite,
    pref.is_pinned,
    'migration_060',
    NOW()
FROM chat_model_preferences AS pref
WHERE pref.provider = 'legacy'
  AND EXISTS (
      SELECT 1
      FROM llm_models AS models
      WHERE models.provider = 'codex'
        AND models.model_id = pref.model_id
  )
ON CONFLICT (preference_key) DO NOTHING;
