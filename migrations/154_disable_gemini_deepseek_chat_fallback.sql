-- Disable Gemini/DeepSeek from general chat and runner LLM fallback paths.
-- CEO policy: use Claude Fable 5.1 with Codex CLI same-grade fallback.

UPDATE llm_models
SET is_selectable = FALSE,
    updated_at = NOW()
WHERE is_selectable IS TRUE
  AND (
    provider IN ('gemini', 'deepseek')
    OR lower(model_id) LIKE '%deepseek%'
  );

UPDATE model_routing_preferences
SET is_enabled = FALSE,
    updated_at = NOW(),
    updated_by = 'migration-154-disable-gemini-deepseek-chat-fallback'
WHERE is_enabled IS TRUE
  AND route_key IN ('llm', 'runner_llm')
  AND (
    provider IN ('gemini', 'deepseek', 'google')
    OR lower(model_id) LIKE '%deepseek%'
    OR lower(model_id) LIKE '%gemini%'
  );
