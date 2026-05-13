-- 095_local_multimodal_model_bridge.sql
-- Prepare CEO PC local non-Ollama model bridges and async media job kinds.

BEGIN;

ALTER TABLE media_generation_jobs
    DROP CONSTRAINT IF EXISTS media_generation_jobs_kind_chk;

ALTER TABLE media_generation_jobs
    ADD CONSTRAINT media_generation_jobs_kind_chk
    CHECK (kind IN ('image', 'edit_image', 'video', 'music', 'model_3d'));

CREATE TEMP TABLE _local_model_seed_models (
    model_id TEXT,
    display_name TEXT,
    bridge TEXT,
    route_key TEXT,
    task TEXT,
    display_order INTEGER
) ON COMMIT DROP;

INSERT INTO _local_model_seed_models VALUES
    ('openai/whisper-large-v3-turbo', 'Local Whisper Large v3 Turbo STT', 'local_audio', 'audio', 'stt', 210),
    ('Qwen/Qwen3-Embedding-0.6B', 'Local Qwen3 Embedding 0.6B', 'local_embedding', 'embedding', 'embedding', 220),
    ('BAAI/bge-m3', 'Local BGE M3 Embedding', 'local_embedding', 'embedding', 'embedding', 222),
    ('Qwen/Qwen3-Reranker-0.6B', 'Local Qwen3 Reranker 0.6B', 'local_rerank', 'rerank', 'rerank', 230),
    ('PaddlePaddle/PaddleOCR-VL', 'Local PaddleOCR-VL Document OCR', 'local_document', 'document', 'ocr_layout', 240),
    ('tesseract-5', 'Local Tesseract 5 OCR', 'local_document', 'document', 'ocr_text', 242),
    ('black-forest-labs/FLUX.2-klein-4B', 'Local FLUX.2 Klein 4B Image', 'local_image', 'image', 'image_generate_edit', 250),
    ('QwenLM/Qwen-Image', 'Local Qwen Image', 'local_image', 'image', 'image_generate_edit', 252),
    ('Tongyi-MAI/Z-Image-Turbo', 'Local Z-Image Turbo', 'local_image', 'image', 'image_generate', 254),
    ('Wan-Video/Wan2.2-TI2V-5B', 'Local Wan2.2 TI2V 5B Video', 'local_video', 'video', 'image_to_video', 260),
    ('Lightricks/LTX-Video', 'Local LTX Video', 'local_video', 'video', 'text_or_image_to_video', 262),
    ('stabilityai/stable-audio-open-1.0', 'Local Stable Audio Open', 'local_music', 'music', 'music_audio_generate', 270),
    ('Tencent-Hunyuan/Hunyuan3D-2.1', 'Local Hunyuan3D 2.1', 'local_3d', 'model_3d', 'image_to_3d', 280);

INSERT INTO llm_models (
    provider, model_id, display_name, family, category,
    supports_tools, supports_thinking, supports_vision, supports_coding,
    input_cost, output_cost, is_active, activation_source,
    linked_key_name, metadata, execution_model_id, discovery_source,
    last_seen_at, retired_at, verification_status, last_verified_at,
    capabilities, pricing, is_selectable, is_executable, updated_at
)
SELECT
    'pc_local', model_id, display_name, bridge, route_key,
    FALSE, FALSE, bridge IN ('local_document', 'local_image', 'local_video', 'local_3d'), FALSE,
    0, 0, FALSE, 'manual',
    NULL,
    jsonb_build_object(
        'execution_backend', 'pc_agent_local_model',
        'bridge', bridge,
        'task', task,
        'canonical_queue', 'scripts/local_model_install_queue.json',
        'async_job', bridge IN ('local_image', 'local_video', 'local_music', 'local_3d'),
        'routing_note', 'Prepared CEO PC local model route. Do not make default until install and benchmark pass.'
    ),
    model_id, 'manual_seed',
    NOW(), NULL, 'queued_not_installed', NULL,
    jsonb_build_object(
        bridge, TRUE,
        'pc_agent', TRUE,
        'local_model_manager', TRUE,
        'async_media_job', bridge IN ('local_image', 'local_video', 'local_music', 'local_3d'),
        'cost', '$0-runtime'
    ),
    '{"input_per_million":0,"output_per_million":0,"currency":"USD"}'::jsonb,
    TRUE, TRUE, NOW()
FROM _local_model_seed_models
ON CONFLICT (provider, model_id)
DO UPDATE SET
    display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category,
    metadata = COALESCE(llm_models.metadata, '{}'::jsonb) || EXCLUDED.metadata,
    capabilities = COALESCE(llm_models.capabilities, '{}'::jsonb) || EXCLUDED.capabilities,
    verification_status = 'queued_not_installed',
    is_active = FALSE,
    is_selectable = TRUE,
    is_executable = TRUE,
    updated_at = NOW();

INSERT INTO model_routing_preferences (
    route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by
)
SELECT
    route_key, 'pc_local', model_id, display_order, FALSE, FALSE,
    'Prepared local PC model. Keep disabled as default until install/test/benchmark succeeds.',
    NOW(), 'migration_095'
FROM _local_model_seed_models
WHERE route_key IN ('image', 'video', 'music', 'model_3d')
ON CONFLICT (route_key, provider, model_id)
DO UPDATE SET
    display_order = EXCLUDED.display_order,
    is_enabled = FALSE,
    is_default = FALSE,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

COMMIT;
