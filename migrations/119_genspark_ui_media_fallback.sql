-- 119_genspark_ui_media_fallback.sql
-- Register browser-driven Genspark UI media fallback routes.

BEGIN;

INSERT INTO llm_models (
    provider,
    model_id,
    display_name,
    family,
    category,
    supports_tools,
    supports_thinking,
    supports_vision,
    supports_coding,
    is_active,
    activation_source,
    linked_key_name,
    metadata,
    execution_model_id,
    discovery_source,
    first_seen_at,
    last_seen_at,
    verification_status,
    capabilities,
    pricing,
    is_selectable,
    is_executable,
    updated_at
)
VALUES
    (
        'genspark_ui',
        'genspark-image-ui',
        'Genspark UI Image Fallback',
        'genspark',
        'media_image',
        false,
        false,
        true,
        false,
        true,
        'manual',
        NULL,
        '{"owner":"ceo","source":"migration_119","execution":"browser_ui","requires_logged_in_browser":true}'::jsonb,
        'genspark-image-ui',
        'manual_seed',
        NOW(),
        NOW(),
        'queued_requires_agent',
        '{"image_generation":true,"provider_family":"genspark","execution_backend":"browser_ui","requires_pc_agent":true,"requires_logged_in_browser":true}'::jsonb,
        '{}'::jsonb,
        true,
        true,
        NOW()
    ),
    (
        'genspark_ui',
        'genspark-video-ui',
        'Genspark UI Video Fallback',
        'genspark',
        'media_video',
        false,
        false,
        true,
        false,
        true,
        'manual',
        NULL,
        '{"owner":"ceo","source":"migration_119","execution":"browser_ui","requires_logged_in_browser":true}'::jsonb,
        'genspark-video-ui',
        'manual_seed',
        NOW(),
        NOW(),
        'queued_requires_agent',
        '{"video_generation":true,"provider_family":"genspark","execution_backend":"browser_ui","requires_pc_agent":true,"requires_logged_in_browser":true}'::jsonb,
        '{}'::jsonb,
        true,
        true,
        NOW()
    )
ON CONFLICT (provider, model_id) DO UPDATE
SET
    display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category,
    is_active = true,
    activation_source = EXCLUDED.activation_source,
    linked_key_name = EXCLUDED.linked_key_name,
    metadata = COALESCE(llm_models.metadata, '{}'::jsonb) || EXCLUDED.metadata,
    execution_model_id = EXCLUDED.execution_model_id,
    discovery_source = EXCLUDED.discovery_source,
    last_seen_at = NOW(),
    verification_status = EXCLUDED.verification_status,
    capabilities = COALESCE(llm_models.capabilities, '{}'::jsonb) || EXCLUDED.capabilities,
    is_selectable = true,
    is_executable = true,
    updated_at = NOW();

INSERT INTO model_routing_preferences (
    route_key,
    provider,
    model_id,
    display_order,
    is_enabled,
    is_default,
    notes,
    updated_at,
    updated_by
)
VALUES
    ('image', 'genspark_ui', 'genspark-image-ui', 90, true, false, 'Browser UI fallback. Queues jobs for logged-in Genspark session automation when API providers are blocked.', NOW(), 'migration_119'),
    ('edit_image', 'genspark_ui', 'genspark-image-ui', 91, true, false, 'Browser UI fallback for image edit requests. Requires PC Agent/Browser Bridge and logged-in Genspark session.', NOW(), 'migration_119'),
    ('video', 'genspark_ui', 'genspark-video-ui', 92, true, false, 'Browser UI fallback. Queues jobs for logged-in Genspark session automation when API video providers are blocked.', NOW(), 'migration_119')
ON CONFLICT (route_key, provider, model_id) DO UPDATE
SET
    display_order = EXCLUDED.display_order,
    is_enabled = true,
    is_default = false,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

COMMIT;
