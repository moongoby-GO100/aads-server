-- Runner AI review must follow runner_model_config.AI_REVIEW in DB order and
-- execute only through Codex/Claude CLI relays.  API/LiteLLM entries are
-- removed without reordering the remaining configured models.
-- Rollback: restore the previous models JSON from the release audit/backup;
-- the application remains fail-closed and will ignore non-CLI entries.
BEGIN;

WITH cli_models AS (
    SELECT COALESCE(
        jsonb_agg(model ORDER BY ord) FILTER (
            WHERE lower(model) LIKE 'codex:gpt-%'
               OR lower(model) LIKE 'claude:%'
               OR lower(model) LIKE 'gpt-%'
               OR lower(model) LIKE 'claude-%'
        ),
        '["codex:gpt-5.6-luna"]'::jsonb
    ) AS models
    FROM runner_model_config AS config
    CROSS JOIN LATERAL jsonb_array_elements_text(config.models)
        WITH ORDINALITY AS configured(model, ord)
    WHERE config.size = 'AI_REVIEW'
)
UPDATE runner_model_config AS config
SET models = cli_models.models,
    updated_at = NOW(),
    updated_by = 'migration-20260921-ai-review-cli-only'
FROM cli_models
WHERE config.size = 'AI_REVIEW'
  AND config.models IS DISTINCT FROM cli_models.models;

COMMIT;
