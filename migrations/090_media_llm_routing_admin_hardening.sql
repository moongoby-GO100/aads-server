-- 090_media_llm_routing_admin_hardening.sql
-- Idempotent hardening for DB-backed media/LLM routing admin visibility.

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

ALTER TABLE model_routing_preferences
    ADD COLUMN IF NOT EXISTS display_order INTEGER NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT 'system';

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_routing_preferences_one_default
    ON model_routing_preferences(route_key)
    WHERE is_default = TRUE;

CREATE INDEX IF NOT EXISTS idx_model_routing_preferences_order
    ON model_routing_preferences(route_key, is_default DESC, is_enabled DESC, display_order ASC);

CREATE TABLE IF NOT EXISTS runner_model_config (
    size TEXT PRIMARY KEY,
    models JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT NOT NULL DEFAULT 'system',
    CONSTRAINT runner_model_config_size_chk
        CHECK (size IN ('XS', 'S', 'M', 'L', 'XL', 'AI_REVIEW'))
);

ALTER TABLE runner_model_config
    ADD COLUMN IF NOT EXISTS models JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT 'system';

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
        ('anthropic', 'claude-opus-4-8', 'Claude Opus 4.8', 'claude', 'coding', TRUE, TRUE, FALSE, TRUE, FALSE, 'manual', 'claude-opus-4-8', 'manual_seed', 'not_configured', TRUE, FALSE, '{"routing_note":"Anthropic runtime uses OAuth auth token via central client/CLI relay; Models API may remain unavailable with OAuth only."}'::jsonb, '{"coding":true,"thinking":true}'::jsonb),
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
    metadata = COALESCE(llm_models.metadata, '{}'::jsonb) || EXCLUDED.metadata,
    execution_model_id = COALESCE(llm_models.execution_model_id, EXCLUDED.execution_model_id),
    discovery_source = COALESCE(NULLIF(llm_models.discovery_source, ''), EXCLUDED.discovery_source),
    last_seen_at = NOW(),
    retired_at = NULL,
    verification_status = CASE
        WHEN llm_models.is_executable OR llm_models.is_active THEN 'verified'
        WHEN llm_models.verification_status IN ('', 'unknown') THEN EXCLUDED.verification_status
        ELSE llm_models.verification_status
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
    ('image', 'openai', 'gpt-image-2', 10, TRUE, FALSE, 'Default image generation route. Returns NOT_CONFIGURED when OpenAI credentials are absent.', NOW(), 'migration_090'),
    ('image', 'google', 'imagen-4.0-generate-001', 20, TRUE, FALSE, 'Imagen 4.0 standard route; prefix family imagen-4.0-* is recognized by MediaGenerationService.', NOW(), 'migration_090'),
    ('image', 'google', 'imagen-4.0-fast-generate-001', 30, TRUE, FALSE, 'Imagen 4.0 fast route.', NOW(), 'migration_090'),
    ('image', 'google', 'imagen-4.0-ultra-generate-001', 40, TRUE, FALSE, 'Imagen 4.0 ultra route.', NOW(), 'migration_090'),
    ('image', 'gemini', 'gemini-3.1-flash-image-preview', 50, FALSE, FALSE, 'Visible in admin; adapter support is not enabled.', NOW(), 'migration_090'),
    ('edit_image', 'openai', 'gpt-image-2', 10, TRUE, FALSE, 'Default image edit route. Requires input image and OpenAI credentials.', NOW(), 'migration_090'),
    ('video', 'openai', 'sora-2', 10, TRUE, FALSE, 'Default video job route. Job is recorded and returns a clear unavailable/not-configured status until adapter is enabled.', NOW(), 'migration_090'),
    ('video', 'openai', 'sora-2-pro', 20, TRUE, FALSE, 'Sora 2 Pro video route; adapter pending.', NOW(), 'migration_090'),
    ('video', 'google', 'veo-3.1-generate-preview', 30, TRUE, FALSE, 'Veo 3.1 video route; adapter pending.', NOW(), 'migration_090'),
    ('llm', 'codex', 'gpt-5.5', 10, TRUE, FALSE, 'Default LLM route visible to admin; Codex runtime is keyless.', NOW(), 'migration_090'),
    ('llm', 'anthropic', 'claude-opus-4-8', 20, TRUE, FALSE, 'Anthropic OAuth runtime model. Uses central auth-token flow, not direct legacy API-key wiring.', NOW(), 'migration_090'),
    ('llm', 'gemini', 'gemini-3.1-pro-preview', 30, TRUE, FALSE, 'Gemini LLM route must execute through LiteLLM proxy.', NOW(), 'migration_090')
ON CONFLICT (route_key, provider, model_id)
DO UPDATE SET
    display_order = CASE
        WHEN model_routing_preferences.updated_by IN ('system', 'migration_089', 'migration_090')
            THEN EXCLUDED.display_order
        ELSE model_routing_preferences.display_order
    END,
    notes = CASE
        WHEN model_routing_preferences.notes = ''
          OR model_routing_preferences.updated_by IN ('system', 'migration_089', 'migration_090')
            THEN EXCLUDED.notes
        ELSE model_routing_preferences.notes
    END,
    updated_at = CASE
        WHEN model_routing_preferences.notes = ''
          OR model_routing_preferences.updated_by IN ('system', 'migration_089', 'migration_090')
            THEN NOW()
        ELSE model_routing_preferences.updated_at
    END,
    updated_by = CASE
        WHEN model_routing_preferences.updated_by IN ('system', 'migration_089', 'migration_090')
            THEN EXCLUDED.updated_by
        ELSE model_routing_preferences.updated_by
    END;

UPDATE model_routing_preferences
SET is_default = TRUE, updated_at = NOW(), updated_by = 'migration_090'
WHERE route_key = 'image'
  AND provider = 'openai'
  AND model_id = 'gpt-image-2'
  AND NOT EXISTS (
      SELECT 1 FROM model_routing_preferences existing
      WHERE existing.route_key = 'image' AND existing.is_default = TRUE
  );

UPDATE model_routing_preferences
SET is_default = TRUE, updated_at = NOW(), updated_by = 'migration_090'
WHERE route_key = 'edit_image'
  AND provider = 'openai'
  AND model_id = 'gpt-image-2'
  AND NOT EXISTS (
      SELECT 1 FROM model_routing_preferences existing
      WHERE existing.route_key = 'edit_image' AND existing.is_default = TRUE
  );

UPDATE model_routing_preferences
SET is_default = TRUE, updated_at = NOW(), updated_by = 'migration_090'
WHERE route_key = 'video'
  AND provider = 'openai'
  AND model_id = 'sora-2'
  AND NOT EXISTS (
      SELECT 1 FROM model_routing_preferences existing
      WHERE existing.route_key = 'video' AND existing.is_default = TRUE
  );

UPDATE model_routing_preferences
SET is_default = TRUE, updated_at = NOW(), updated_by = 'migration_090'
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
    ('codex:gpt-5.5', 'codex', 'gpt-5.5', 10, FALSE, TRUE, TRUE, 'migration_090', NOW()),
    ('anthropic:claude-opus-4-8', 'anthropic', 'claude-opus-4-8', 20, FALSE, TRUE, FALSE, 'migration_090', NOW()),
    ('gemini:gemini-3.1-pro-preview', 'gemini', 'gemini-3.1-pro-preview', 30, FALSE, TRUE, FALSE, 'migration_090', NOW())
ON CONFLICT (preference_key)
DO UPDATE SET
    provider = EXCLUDED.provider,
    model_id = EXCLUDED.model_id,
    display_order = CASE
        WHEN chat_model_preferences.updated_by IN ('system', 'migration_089', 'migration_090')
            THEN EXCLUDED.display_order
        ELSE chat_model_preferences.display_order
    END,
    is_hidden = FALSE,
    is_favorite = chat_model_preferences.is_favorite OR EXCLUDED.is_favorite,
    is_pinned = chat_model_preferences.is_pinned OR EXCLUDED.is_pinned,
    updated_by = CASE
        WHEN chat_model_preferences.updated_by IN ('system', 'migration_089', 'migration_090')
            THEN EXCLUDED.updated_by
        ELSE chat_model_preferences.updated_by
    END,
    updated_at = CASE
        WHEN chat_model_preferences.updated_by IN ('system', 'migration_089', 'migration_090')
            THEN NOW()
        ELSE chat_model_preferences.updated_at
    END;

INSERT INTO runner_model_config (size, models, updated_at, updated_by)
VALUES
    ('XS', '["codex:gpt-5.5","litellm:gemini-3.1-pro-preview","claude-opus-4-8"]'::jsonb, NOW(), 'migration_090'),
    ('S', '["codex:gpt-5.5","litellm:gemini-3.1-pro-preview","claude-opus-4-8"]'::jsonb, NOW(), 'migration_090'),
    ('M', '["codex:gpt-5.5","litellm:gemini-3.1-pro-preview","claude-opus-4-8"]'::jsonb, NOW(), 'migration_090'),
    ('L', '["codex:gpt-5.5","claude-opus-4-8","litellm:gemini-3.1-pro-preview"]'::jsonb, NOW(), 'migration_090'),
    ('XL', '["claude-opus-4-8","codex:gpt-5.5","litellm:gemini-3.1-pro-preview"]'::jsonb, NOW(), 'migration_090'),
    ('AI_REVIEW', '["codex:gpt-5.5","claude-opus-4-8","litellm:gemini-3.1-pro-preview"]'::jsonb, NOW(), 'migration_090')
ON CONFLICT (size) DO NOTHING;

COMMIT;
