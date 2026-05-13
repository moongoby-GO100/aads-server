-- 092_pc_ollama_gemma4_bridge.sql
-- Register CEO PC Ollama Gemma 4 models and route metadata.

BEGIN;

WITH seed_models (
    provider, model_id, display_name, family, category,
    supports_tools, supports_thinking, supports_vision, supports_coding,
    is_active, is_selectable, is_executable, verification_status,
    execution_model_id, metadata, capabilities
) AS (
    VALUES
        (
            'pc_ollama', 'gemma4:e4b', 'Gemma 4 E4B (CEO PC Ollama)',
            'gemma4', 'local_llm',
            FALSE, FALSE, TRUE, FALSE,
            TRUE, TRUE, TRUE, 'pending_verification',
            'gemma4:e4b',
            '{
                "execution_backend":"pc_ollama",
                "execution_model_id":"gemma4:e4b",
                "timeout_seconds":300,
                "max_tokens":2048,
                "routing_note":"Executes through connected CEO PC Agent Ollama bridge. Use for low-cost drafts, summaries and lightweight classification after local install verification.",
                "hardware_note":"Target PC has RTX 3060 12GB VRAM in May 2026 measurement; E4B is the primary local model candidate."
            }'::jsonb,
            '{"local_llm":true,"pc_ollama":true,"vision":true,"cost":"$0-runtime"}'::jsonb
        ),
        (
            'pc_ollama', 'gemma4:26b', 'Gemma 4 26B A4B (CEO PC Ollama Compare)',
            'gemma4', 'local_llm_compare',
            FALSE, FALSE, TRUE, FALSE,
            FALSE, TRUE, FALSE, 'comparison_only',
            'gemma4:26b',
            '{
                "execution_backend":"pc_ollama",
                "execution_model_id":"gemma4:26b",
                "timeout_seconds":600,
                "max_tokens":2048,
                "routing_note":"Comparison-only route. Enable only for explicit benchmark runs because 26B A4B is expected to pressure 12GB VRAM.",
                "hardware_note":"Do not make this default on RTX 3060 12GB without speed and memory verification."
            }'::jsonb,
            '{"local_llm":true,"pc_ollama":true,"vision":true,"comparison_only":true,"cost":"$0-runtime"}'::jsonb
        )
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
    0, 0, is_active, 'manual',
    NULL, metadata, execution_model_id, 'manual_seed',
    NOW(), NULL, verification_status, NULL,
    capabilities, '{"input_per_million":0,"output_per_million":0,"currency":"USD"}'::jsonb,
    is_selectable, is_executable, NOW()
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
    input_cost = EXCLUDED.input_cost,
    output_cost = EXCLUDED.output_cost,
    is_active = EXCLUDED.is_active,
    activation_source = 'manual',
    metadata = COALESCE(llm_models.metadata, '{}'::jsonb) || EXCLUDED.metadata,
    execution_model_id = EXCLUDED.execution_model_id,
    discovery_source = EXCLUDED.discovery_source,
    last_seen_at = NOW(),
    retired_at = NULL,
    verification_status = EXCLUDED.verification_status,
    capabilities = COALESCE(llm_models.capabilities, '{}'::jsonb) || EXCLUDED.capabilities,
    pricing = EXCLUDED.pricing,
    is_selectable = EXCLUDED.is_selectable,
    is_executable = EXCLUDED.is_executable,
    updated_at = NOW();

INSERT INTO model_routing_preferences (
    route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by
)
VALUES
    ('llm', 'pc_ollama', 'gemma4:e4b', 80, TRUE, FALSE, 'CEO PC Ollama primary local model after install verification.', NOW(), 'migration_092'),
    ('llm', 'pc_ollama', 'gemma4:26b', 90, FALSE, FALSE, 'Comparison-only route; keep disabled unless explicitly benchmarking.', NOW(), 'migration_092')
ON CONFLICT (route_key, provider, model_id)
DO UPDATE SET
    display_order = EXCLUDED.display_order,
    is_enabled = EXCLUDED.is_enabled,
    is_default = FALSE,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

INSERT INTO chat_model_preferences (
    preference_key, provider, model_id, display_order, is_hidden, is_favorite, is_pinned, updated_by, updated_at
)
VALUES
    ('pc_ollama:gemma4:e4b', 'pc_ollama', 'gemma4:e4b', 80, FALSE, TRUE, FALSE, 'migration_092', NOW()),
    ('pc_ollama:gemma4:26b', 'pc_ollama', 'gemma4:26b', 90, TRUE, FALSE, FALSE, 'migration_092', NOW())
ON CONFLICT (preference_key)
DO UPDATE SET
    provider = EXCLUDED.provider,
    model_id = EXCLUDED.model_id,
    display_order = EXCLUDED.display_order,
    is_hidden = EXCLUDED.is_hidden,
    is_favorite = EXCLUDED.is_favorite,
    is_pinned = FALSE,
    updated_by = EXCLUDED.updated_by,
    updated_at = NOW();

COMMIT;
