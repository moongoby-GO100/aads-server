-- 152: Align chat LLM routing with CEO-approved Fable 5.1 <-> Codex CLI fallback.
-- Scope: chat/text LLM selection and fallback routing only. Media/search-specific Gemini routes remain intact.

UPDATE llm_models
SET
    is_selectable = FALSE,
    updated_at = NOW(),
    metadata = COALESCE(metadata, '{}'::jsonb)
        || '{"chat_selection_disabled_reason":"CEO directive 2026-09-06: remove Gemini/DeepSeek from chat fallback chain; use Claude/Codex CLI instead"}'::jsonb
WHERE provider IN ('gemini', 'deepseek')
  AND category IN ('general', 'reasoning')
  AND is_selectable IS DISTINCT FROM FALSE;

UPDATE llm_models
SET
    tier = 'A',
    fallback_group = 'premium',
    is_selectable = TRUE,
    is_executable = TRUE,
    updated_at = NOW(),
    metadata = COALESCE(metadata, '{}'::jsonb)
        || '{"routing_note":"Highest-quality Claude chat model; fallback peer is Codex CLI gpt-5.6-sol"}'::jsonb
WHERE provider = 'anthropic'
  AND model_id = 'claude-fable-5-1';

INSERT INTO model_routing_preferences (
    route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by,
    display_name, family, category
)
VALUES (
    'llm', 'anthropic', 'claude-fable-5-1', 1, TRUE, FALSE,
    'CEO directive 2026-09-06: highest-quality Claude peer for Codex CLI fallback',
    NOW(), 'migration_152_chat_llm_fable_codex_fallback',
    'Claude Fable 5.1', 'claude', 'reasoning'
)
ON CONFLICT (route_key, provider, model_id) DO UPDATE
SET
    display_order = 1,
    is_enabled = TRUE,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by,
    display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category;

UPDATE model_routing_preferences
SET
    is_enabled = FALSE,
    notes = 'Disabled for chat LLM fallback by CEO directive 2026-09-06; use Claude/Codex CLI chain',
    updated_at = NOW(),
    updated_by = 'migration_152_chat_llm_fable_codex_fallback'
WHERE route_key = 'llm'
  AND provider IN ('gemini', 'google', 'deepseek');
