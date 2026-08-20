-- P1: make admin runner defaults use Codex CLI gpt-5.6 family without shell fallback.
-- M -> Luna, L -> Sol, XL/AI_REVIEW -> Terra first.

INSERT INTO runner_model_config (size, models, updated_at, updated_by)
VALUES
    ('M', '["codex:gpt-5.6-luna","claude-sonnet-4-6","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-126-runner-codex56'),
    ('L', '["codex:gpt-5.6-sol","codex:gpt-5.6-terra","claude-sonnet-4-6","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-126-runner-codex56'),
    ('XL', '["codex:gpt-5.6-terra","codex:gpt-5.6-sol","claude-opus-5","claude-sonnet-4-6","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-126-runner-codex56'),
    ('AI_REVIEW', '["codex:gpt-5.6-terra","claude-sonnet-4-6","claude-haiku-4-5-20251001","codex:gpt-5.5"]'::jsonb, NOW(), 'migration-126-runner-codex56')
ON CONFLICT (size) DO UPDATE
SET models = EXCLUDED.models,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;
