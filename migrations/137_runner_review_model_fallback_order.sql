-- P1: Align Pipeline Runner model priority with the settings review model order.
-- The runner now uses size config first, then AI_REVIEW, runner_llm, and llm routing.

BEGIN;

INSERT INTO runner_model_config (size, models, updated_at, updated_by)
VALUES
    ('XS', '["codex:gpt-5.6-sol","codex:gpt-5.6-terra","codex:gpt-5.6-luna","claude-opus-5","claude-sonnet-4-6","claude-haiku-4-5-20251001","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-137-runner-review-order'),
    ('S', '["codex:gpt-5.6-sol","codex:gpt-5.6-terra","codex:gpt-5.6-luna","claude-opus-5","claude-sonnet-4-6","claude-haiku-4-5-20251001","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-137-runner-review-order'),
    ('M', '["codex:gpt-5.6-sol","codex:gpt-5.6-terra","codex:gpt-5.6-luna","claude-opus-5","claude-sonnet-4-6","claude-haiku-4-5-20251001","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-137-runner-review-order'),
    ('L', '["codex:gpt-5.6-sol","codex:gpt-5.6-terra","codex:gpt-5.6-luna","claude-opus-5","claude-sonnet-4-6","claude-haiku-4-5-20251001","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-137-runner-review-order'),
    ('XL', '["codex:gpt-5.6-sol","codex:gpt-5.6-terra","codex:gpt-5.6-luna","claude-opus-5","claude-sonnet-4-6","claude-haiku-4-5-20251001","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-137-runner-review-order'),
    ('AI_REVIEW', '["codex:gpt-5.6-sol","codex:gpt-5.6-terra","codex:gpt-5.6-luna","claude-opus-5","claude-sonnet-4-6","claude-haiku-4-5-20251001","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-137-runner-review-order')
ON CONFLICT (size) DO UPDATE
SET models = EXCLUDED.models,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

UPDATE model_routing_preferences
SET is_default = FALSE,
    updated_at = NOW(),
    updated_by = 'migration-137-runner-review-order'
WHERE route_key = 'runner_llm';

INSERT INTO model_routing_preferences
    (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
    ('runner_llm','codex','gpt-5.6-sol',5,true,true,'Runner/review primary from settings order',NOW(),'migration-137-runner-review-order'),
    ('runner_llm','codex','gpt-5.6-terra',6,true,false,'Runner/review fallback from settings order',NOW(),'migration-137-runner-review-order'),
    ('runner_llm','codex','gpt-5.6-luna',7,true,false,'Runner/review fallback from settings order',NOW(),'migration-137-runner-review-order'),
    ('runner_llm','anthropic','claude-opus-5',10,true,false,'Claude fallback after Codex 5.6 family',NOW(),'migration-137-runner-review-order'),
    ('runner_llm','openai','gpt-5.5',20,true,false,'GPT-5.5 legacy backup',NOW(),'migration-137-runner-review-order'),
    ('runner_llm','google','gemini-2.5-pro',30,true,false,'Gemini 2.5 Pro backup',NOW(),'migration-137-runner-review-order')
ON CONFLICT (route_key, provider, model_id) DO UPDATE
SET display_order = EXCLUDED.display_order,
    is_enabled = EXCLUDED.is_enabled,
    is_default = EXCLUDED.is_default,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

UPDATE model_routing_preferences
SET display_order = 100 + display_order,
    is_default = FALSE,
    notes = CASE
        WHEN notes = '' THEN 'Legacy runner fallback kept after review-order models'
        ELSE notes
    END,
    updated_at = NOW(),
    updated_by = 'migration-137-runner-review-order'
WHERE route_key = 'runner_llm'
  AND NOT (
      provider = 'codex' AND model_id IN ('gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')
      OR provider = 'anthropic' AND model_id = 'claude-opus-5'
      OR provider = 'openai' AND model_id = 'gpt-5.5'
      OR provider = 'google' AND model_id = 'gemini-2.5-pro'
  )
  AND display_order < 100;

COMMIT;
