-- 155: Add GPT-6 Astra to Pipeline Runner size model cycles.
-- Keeps existing CEO order for XS/S/M/L/AI_REVIEW and places Astra directly
-- after Fable 5.1 for XL so it is the same-grade Codex CLI fallback.

UPDATE runner_model_config
SET
    models = CASE
        WHEN size = 'XL' THEN (
            SELECT jsonb_agg(value)
            FROM (
                SELECT value, MIN(ord) AS first_ord
                FROM (
                    SELECT value, ord
                    FROM jsonb_array_elements_text(
                        jsonb_build_array('claude-fable-5-1', 'codex:gpt-6-astra')
                        || (models - 'claude-fable-5-1' - 'codex:gpt-6-astra')
                    ) WITH ORDINALITY AS t(value, ord)
                ) dedupe_source
                GROUP BY value
                ORDER BY MIN(ord)
            ) deduped
        )
        WHEN models ? 'codex:gpt-6-astra' THEN models
        ELSE models || '["codex:gpt-6-astra"]'::jsonb
    END,
    updated_at = NOW(),
    updated_by = 'migration_155_runner_model_config_gpt6_astra'
WHERE size IN ('XS', 'S', 'M', 'L', 'XL', 'AI_REVIEW');
