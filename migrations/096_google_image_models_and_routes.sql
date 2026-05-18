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
    is_executable
)
VALUES
    (
        'gemini',
        'gemini-2.5-flash-image',
        'Nano Banana',
        'gemini-image',
        'media_image',
        false,
        true,
        true,
        false,
        true,
        'manual',
        'GEMINI_API_KEY',
        '{"owner":"ceo","source":"migration_096"}'::jsonb,
        'gemini-2.5-flash-image',
        'manual_seed',
        NOW(),
        NOW(),
        'verified',
        '{"image_generation":true,"image_editing":true,"provider_family":"nano_banana"}'::jsonb,
        '{}'::jsonb,
        true,
        true
    ),
    (
        'gemini',
        'gemini-3.1-flash-image-preview',
        'Nano Banana 2',
        'gemini-image',
        'media_image',
        false,
        true,
        true,
        false,
        true,
        'manual',
        'GEMINI_API_KEY',
        '{"owner":"ceo","source":"migration_096"}'::jsonb,
        'gemini-3.1-flash-image-preview',
        'manual_seed',
        NOW(),
        NOW(),
        'review_required',
        '{"image_generation":true,"image_editing":true,"provider_family":"nano_banana_2"}'::jsonb,
        '{}'::jsonb,
        true,
        true
    ),
    (
        'gemini',
        'gemini-3-pro-image-preview',
        'Nano Banana Pro',
        'gemini-image',
        'media_image',
        false,
        true,
        true,
        false,
        true,
        'manual',
        'GEMINI_API_KEY',
        '{"owner":"ceo","source":"migration_096"}'::jsonb,
        'gemini-3-pro-image-preview',
        'manual_seed',
        NOW(),
        NOW(),
        'review_required',
        '{"image_generation":true,"image_editing":true,"provider_family":"nano_banana_pro"}'::jsonb,
        '{}'::jsonb,
        true,
        true
    ),
    (
        'google',
        'imagen-4.0-generate-001',
        'Imagen 4',
        'imagen',
        'media_image',
        false,
        false,
        true,
        false,
        true,
        'manual',
        'GEMINI_API_KEY',
        '{"owner":"ceo","source":"migration_096"}'::jsonb,
        'imagen-4.0-generate-001',
        'manual_seed',
        NOW(),
        NOW(),
        'verified',
        '{"image_generation":true,"provider_family":"imagen"}'::jsonb,
        '{}'::jsonb,
        true,
        true
    ),
    (
        'google',
        'imagen-4.0-fast-generate-001',
        'Imagen 4 Fast',
        'imagen',
        'media_image',
        false,
        false,
        true,
        false,
        true,
        'manual',
        'GEMINI_API_KEY',
        '{"owner":"ceo","source":"migration_096"}'::jsonb,
        'imagen-4.0-fast-generate-001',
        'manual_seed',
        NOW(),
        NOW(),
        'verified',
        '{"image_generation":true,"provider_family":"imagen_fast"}'::jsonb,
        '{}'::jsonb,
        true,
        true
    ),
    (
        'google',
        'imagen-4.0-ultra-generate-001',
        'Imagen 4 Ultra',
        'imagen',
        'media_image',
        false,
        false,
        true,
        false,
        true,
        'manual',
        'GEMINI_API_KEY',
        '{"owner":"ceo","source":"migration_096"}'::jsonb,
        'imagen-4.0-ultra-generate-001',
        'manual_seed',
        NOW(),
        NOW(),
        'verified',
        '{"image_generation":true,"provider_family":"imagen_ultra"}'::jsonb,
        '{}'::jsonb,
        true,
        true
    )
ON CONFLICT (provider, model_id) DO UPDATE
SET
    display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category,
    is_active = true,
    activation_source = 'manual',
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
    ('image', 'gemini', 'gemini-2.5-flash-image', 10, true, false, 'Nano Banana 안정 경로', NOW(), 'migration_096'),
    ('image', 'gemini', 'gemini-3.1-flash-image-preview', 20, true, false, 'Nano Banana 2 비교 경로', NOW(), 'migration_096'),
    ('image', 'gemini', 'gemini-3-pro-image-preview', 30, true, false, 'Nano Banana Pro 비교 경로', NOW(), 'migration_096'),
    ('image', 'google', 'imagen-4.0-generate-001', 40, true, false, 'Imagen 4 기본 경로', NOW(), 'migration_096'),
    ('image', 'google', 'imagen-4.0-fast-generate-001', 50, true, false, 'Imagen 4 Fast 경로', NOW(), 'migration_096'),
    ('image', 'google', 'imagen-4.0-ultra-generate-001', 60, true, false, 'Imagen 4 Ultra 경로', NOW(), 'migration_096'),
    ('edit_image', 'openai', 'gpt-image-2', 10, false, false, 'OPENAI 비활성 상태. Gemini 편집 어댑터 구현 전까지 비활성 유지', NOW(), 'migration_096')
ON CONFLICT (route_key, provider, model_id) DO UPDATE
SET
    display_order = EXCLUDED.display_order,
    is_enabled = EXCLUDED.is_enabled,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

UPDATE model_routing_preferences
SET
    is_default = CASE
        WHEN route_key = 'image' AND provider = 'google' AND model_id = 'imagen-4.0-generate-001' THEN true
        WHEN route_key = 'edit_image' AND provider = 'openai' AND model_id = 'gpt-image-2' THEN true
        ELSE false
    END,
    updated_at = NOW(),
    updated_by = 'migration_096'
WHERE route_key IN ('image', 'edit_image');

UPDATE model_routing_preferences
SET
    is_enabled = false,
    notes = 'OPENAI_API_KEY inactive; CEO requested OpenAI excluded',
    updated_at = NOW(),
    updated_by = 'migration_096'
WHERE provider = 'openai'
  AND model_id = 'gpt-image-2'
  AND route_key IN ('image', 'edit_image');

COMMIT;
