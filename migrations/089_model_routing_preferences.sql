-- 089_model_routing_preferences.sql
-- DB-backed media/LLM model routing defaults for admin and MediaGenerationService.

BEGIN;

CREATE TABLE IF NOT EXISTS model_routing_preferences (
    route_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 100,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT NOT NULL DEFAULT 'system',
    PRIMARY KEY (route_key, provider, model_id),
    CONSTRAINT model_routing_preferences_route_key_chk
        CHECK (route_key IN ('image', 'edit_image', 'video', 'llm'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_routing_preferences_one_default
    ON model_routing_preferences(route_key)
    WHERE is_default = TRUE;

CREATE INDEX IF NOT EXISTS idx_model_routing_preferences_order
    ON model_routing_preferences(route_key, is_default DESC, is_enabled DESC, display_order ASC);

WITH seed_models (
    provider, model_id, display_name, family, category,
    supports_tools, supports_thinking, supports_vision, supports_coding,
    is_active, activation_source, execution_model_id, discovery_source,
    verification_status, is_selectable, is_executable, metadata, capabilities
) AS (
    VALUES
        ('openai', 'gpt-image-2', 'GPT Image 2', 'image', 'media_image', FALSE, FALSE, TRUE, FALSE, FALSE, 'manual', 'gpt-image-2', 'manual_seed', 'not_configured', TRUE, FALSE, '{"routing_note":"Requires OPENAI_API_KEY and image adapter support."}'::jsonb, '{"image":true,"edit_image":true}'::jsonb),
        ('google', 'imagen-4.0-generate-001', 'Imagen 4.0 Generate', 'imagen', 'media_image', FALSE, FALSE, TRUE, FALSE, FALSE, 'manual', 'imagen-4.0-generate-001', 'manual_seed', 'not_configured', TRUE, FALSE, '{"routing_note":"Requires GOOGLE_API_KEY for Imagen image generation."}'::jsonb, '{"image":true,"prefix_family":"imagen-4.0-*"}'::jsonb),
        ('google', 'imagen-4.0-fast-generate-001', 'Imagen 4.0 Fast Generate', 'imagen', 'media_image', FALSE, FALSE, TRUE, FALSE, FALSE, 'manual', 'imagen-4.0-fast-generate-001', 'manual_seed', 'not_configured', TRUE, FALSE, '{"routing_note":"Requires GOOGLE_API_KEY for Imagen image generation."}'::jsonb, '{"image":true,"prefix_family":"imagen-4.0-*"}'::jsonb),
        ('google', 'imagen-4.0-ultra-generate-001', 'Imagen 4.0 Ultra Generate', 'imagen', 'media_image', FALSE, FALSE, TRUE, FALSE, FALSE, 'manual', 'imagen-4.0-ultra-generate-001', 'manual_seed', 'not_configured', TRUE, FALSE, '{"routing_note":"Requires GOOGLE_API_KEY for Imagen image generation."}'::jsonb, '{"image":true,"prefix_family":"imagen-4.0-*"}'::jsonb),
        ('gemini', 'gemini-3.1-flash-image-preview', 'Gemini 3.1 Flash Image Preview', 'gemini', 'media_image', FALSE, FALSE, TRUE, FALSE, FALSE, 'manual', 'gemini-3.1-flash-image-preview', 'manual_seed', 'review_required', TRUE, FALSE, '{"routing_note":"Catalogued for admin visibility; MediaGenerationService image adapter does not execute this provider yet."}'::jsonb, '{"image":true,"adapter_pending":true}'::jsonb),
        ('openai', 'sora-2', 'Sora 2', 'sora', 'media_video', FALSE, FALSE, TRUE, FALSE, FALSE, 'manual', 'sora-2', 'manual_seed', 'review_required', TRUE, FALSE, '{"routing_note":"Video job routing is recorded; execution adapter returns PROVIDER_UNAVAILABLE until enabled."}'::jsonb, '{"video":true,"adapter_pending":true}'::jsonb),
        ('openai', 'sora-2-pro', 'Sora 2 Pro', 'sora', 'media_video', FALSE, FALSE, TRUE, FALSE, FALSE, 'manual', 'sora-2-pro', 'manual_seed', 'review_required', TRUE, FALSE, '{"routing_note":"Video job routing is recorded; execution adapter returns PROVIDER_UNAVAILABLE until enabled."}'::jsonb, '{"video":true,"adapter_pending":true}'::jsonb),
        ('google', 'veo-3.1-generate-preview', 'Veo 3.1 Generate Preview', 'veo', 'media_video', FALSE, FALSE, TRUE, FALSE, FALSE, 'manual', 'veo-3.1-generate-preview', 'manual_seed', 'review_required', TRUE, FALSE, '{"routing_note":"Video job routing is recorded; execution adapter returns PROVIDER_UNAVAILABLE until enabled."}'::jsonb, '{"video":true,"adapter_pending":true}'::jsonb),
        ('codex', 'gpt-5.5', 'GPT-5.5 (Codex CLI)', 'codex', 'coding', TRUE, TRUE, FALSE, TRUE, TRUE, 'manual', 'gpt-5.5', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Codex CLI model; keyless runtime."}'::jsonb, '{"coding":true,"thinking":true}'::jsonb),
        ('anthropic', 'claude-opus-4-7', 'Claude Opus 4.7', 'claude', 'coding', TRUE, TRUE, FALSE, TRUE, FALSE, 'manual', 'claude-opus-4-7', 'manual_seed', 'not_configured', TRUE, FALSE, '{"routing_note":"Anthropic runtime uses OAuth auth token via central client/CLI relay; Models API may remain unavailable with OAuth only."}'::jsonb, '{"coding":true,"thinking":true}'::jsonb),
        ('gemini', 'gemini-3.1-pro-preview', 'Gemini 3.1 Pro Preview', 'gemini', 'reasoning', TRUE, TRUE, TRUE, TRUE, FALSE, 'manual', 'gemini-3.1-pro-preview', 'manual_seed', 'not_configured', TRUE, FALSE, '{"routing_note":"LLM execution must go through LiteLLM proxy."}'::jsonb, '{"thinking":true,"vision":true,"coding":true}'::jsonb)
)
INSERT INTO llm_models (
    provider, model_id, display_name, family, category,
    supports_tools, supports_thinking, supports_vision, supports_coding,
    input_cost, output_cost, is_active, activation_source,
    linked_key_name, metadata, execution_model_id, discovery_source,
    last_seen_at, retired_at, verification_status, last_verified_at,
    capabilities, pricing, is_selectable, is_executable, updated_at
)
SELECT
    provider, model_id, display_name, family, category,
    supports_tools, supports_thinking, supports_vision, supports_coding,
    NULL, NULL, is_active, activation_source,
    NULL, metadata, execution_model_id, discovery_source,
    NOW(), NULL, verification_status, NULL,
    capabilities, '{}'::jsonb, is_selectable, is_executable, NOW()
FROM seed_models
ON CONFLICT (provider, model_id)
DO UPDATE SET
    display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category,
    supports_tools = EXCLUDED.supports_tools,
    supports_thinking = EXCLUDED.supports_thinking,
    supports_vision = EXCLUDED.supports_vision,
    supports_coding = EXCLUDED.supports_coding,
    activation_source = CASE
        WHEN llm_models.activation_source = 'manual' THEN llm_models.activation_source
        ELSE EXCLUDED.activation_source
    END,
    metadata = COALESCE(llm_models.metadata, '{}'::jsonb) || EXCLUDED.metadata,
    execution_model_id = COALESCE(llm_models.execution_model_id, EXCLUDED.execution_model_id),
    discovery_source = COALESCE(NULLIF(llm_models.discovery_source, ''), EXCLUDED.discovery_source),
    last_seen_at = NOW(),
    retired_at = NULL,
    verification_status = CASE
        WHEN llm_models.is_executable OR llm_models.is_active THEN 'verified'
        ELSE EXCLUDED.verification_status
    END,
    capabilities = COALESCE(llm_models.capabilities, '{}'::jsonb) || EXCLUDED.capabilities,
    pricing = COALESCE(llm_models.pricing, '{}'::jsonb),
    is_selectable = llm_models.is_selectable OR EXCLUDED.is_selectable,
    is_executable = llm_models.is_executable OR EXCLUDED.is_executable,
    is_active = llm_models.is_active OR EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO model_routing_preferences (
    route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by
)
VALUES
    ('image', 'openai', 'gpt-image-2', 10, TRUE, FALSE, 'Default image generation route. Returns NOT_CONFIGURED when OpenAI credentials are absent.', NOW(), 'migration_089'),
    ('image', 'google', 'imagen-4.0-generate-001', 20, TRUE, FALSE, 'Imagen 4.0 standard route; prefix family imagen-4.0-* is recognized by MediaGenerationService.', NOW(), 'migration_089'),
    ('image', 'google', 'imagen-4.0-fast-generate-001', 30, TRUE, FALSE, 'Imagen 4.0 fast route.', NOW(), 'migration_089'),
    ('image', 'google', 'imagen-4.0-ultra-generate-001', 40, TRUE, FALSE, 'Imagen 4.0 ultra route.', NOW(), 'migration_089'),
    ('image', 'gemini', 'gemini-3.1-flash-image-preview', 50, FALSE, FALSE, 'Visible in admin; adapter support is not enabled.', NOW(), 'migration_089'),
    ('edit_image', 'openai', 'gpt-image-2', 10, TRUE, FALSE, 'Default image edit route. Requires input image and OpenAI credentials.', NOW(), 'migration_089'),
    ('video', 'openai', 'sora-2', 10, TRUE, FALSE, 'Default video job route. Job is recorded and returns a clear unavailable/not-configured status until adapter is enabled.', NOW(), 'migration_089'),
    ('video', 'openai', 'sora-2-pro', 20, TRUE, FALSE, 'Sora 2 Pro video route; adapter pending.', NOW(), 'migration_089'),
    ('video', 'google', 'veo-3.1-generate-preview', 30, TRUE, FALSE, 'Veo 3.1 video route; adapter pending.', NOW(), 'migration_089'),
    ('llm', 'codex', 'gpt-5.5', 10, TRUE, FALSE, 'Default LLM route visible to admin; Codex runtime is keyless.', NOW(), 'migration_089'),
    ('llm', 'anthropic', 'claude-opus-4-7', 20, TRUE, FALSE, 'Anthropic OAuth runtime model. Uses central auth-token flow, not direct legacy API-key wiring.', NOW(), 'migration_089'),
    ('llm', 'gemini', 'gemini-3.1-pro-preview', 30, TRUE, FALSE, 'Gemini LLM route must execute through LiteLLM proxy.', NOW(), 'migration_089')
ON CONFLICT (route_key, provider, model_id)
DO UPDATE SET
    display_order = EXCLUDED.display_order,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

UPDATE model_routing_preferences
SET is_default = TRUE, updated_at = NOW(), updated_by = 'migration_089'
WHERE route_key = 'image'
  AND provider = 'openai'
  AND model_id = 'gpt-image-2'
  AND NOT EXISTS (
      SELECT 1 FROM model_routing_preferences existing
      WHERE existing.route_key = 'image' AND existing.is_default = TRUE
  );

UPDATE model_routing_preferences
SET is_default = TRUE, updated_at = NOW(), updated_by = 'migration_089'
WHERE route_key = 'edit_image'
  AND provider = 'openai'
  AND model_id = 'gpt-image-2'
  AND NOT EXISTS (
      SELECT 1 FROM model_routing_preferences existing
      WHERE existing.route_key = 'edit_image' AND existing.is_default = TRUE
  );

UPDATE model_routing_preferences
SET is_default = TRUE, updated_at = NOW(), updated_by = 'migration_089'
WHERE route_key = 'video'
  AND provider = 'openai'
  AND model_id = 'sora-2'
  AND NOT EXISTS (
      SELECT 1 FROM model_routing_preferences existing
      WHERE existing.route_key = 'video' AND existing.is_default = TRUE
  );

UPDATE model_routing_preferences
SET is_default = TRUE, updated_at = NOW(), updated_by = 'migration_089'
WHERE route_key = 'llm'
  AND provider = 'codex'
  AND model_id = 'gpt-5.5'
  AND NOT EXISTS (
      SELECT 1 FROM model_routing_preferences existing
      WHERE existing.route_key = 'llm' AND existing.is_default = TRUE
  );

INSERT INTO chat_model_preferences (
    preference_key, provider, model_id, display_order, is_hidden, is_favorite, is_pinned, updated_by, updated_at
)
VALUES
    ('codex:gpt-5.5', 'codex', 'gpt-5.5', 10, FALSE, TRUE, TRUE, 'migration_089', NOW()),
    ('anthropic:claude-opus-4-7', 'anthropic', 'claude-opus-4-7', 20, FALSE, TRUE, FALSE, 'migration_089', NOW()),
    ('gemini:gemini-3.1-pro-preview', 'gemini', 'gemini-3.1-pro-preview', 30, FALSE, TRUE, FALSE, 'migration_089', NOW())
ON CONFLICT (preference_key)
DO UPDATE SET
    provider = EXCLUDED.provider,
    model_id = EXCLUDED.model_id,
    display_order = EXCLUDED.display_order,
    is_hidden = EXCLUDED.is_hidden,
    is_favorite = EXCLUDED.is_favorite,
    is_pinned = chat_model_preferences.is_pinned OR EXCLUDED.is_pinned,
    updated_by = EXCLUDED.updated_by,
    updated_at = NOW();

COMMIT;
