-- 183: Replace the unsupported XS runner model with the S primary model.
-- Rollback: use PUT /settings/runner-models to restore XS to
-- ["antigravity-flash", "claude-haiku", "codex:gpt-6-luna",
--  "gemini-3.8-flash-medium", "claude-sonnet-5-5", "codex:gpt-6.1-sol"].

UPDATE runner_model_config
SET models = '["codex:gpt-6.1-terra", "codex:gpt-6-luna", "claude-haiku", "gemini-3.8-flash-medium", "claude-sonnet-5-5", "codex:gpt-6.1-sol"]'::jsonb,
    updated_at = NOW(),
    updated_by = 'migration:runner-xs-drop-antigravity-flash'
WHERE size = 'XS'
  AND models ? 'antigravity-flash';
