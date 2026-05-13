-- 093_pc_ollama_quantized_model_pack.sql
-- Register RTX 3060-friendly local Ollama models behind the PC Agent backend.

BEGIN;

CREATE TEMP TABLE _pc_ollama_seed_models (
    provider TEXT, model_id TEXT, display_name TEXT, family TEXT, category TEXT,
    supports_tools BOOLEAN, supports_thinking BOOLEAN, supports_vision BOOLEAN, supports_coding BOOLEAN,
    is_active BOOLEAN, is_selectable BOOLEAN, is_executable BOOLEAN, verification_status TEXT,
    execution_model_id TEXT, canonical_model TEXT, timeout_seconds INTEGER, max_tokens INTEGER,
    display_order INTEGER, is_hidden BOOLEAN, is_favorite BOOLEAN, routing_enabled BOOLEAN, notes TEXT
) ON COMMIT DROP;

INSERT INTO _pc_ollama_seed_models VALUES
    ('litellm', 'pc-gemma4-e2b', 'PC Gemma4 E2B Fast', 'gemma4', 'local_llm_fast', FALSE, FALSE, TRUE, FALSE, TRUE, TRUE, TRUE, 'pending_benchmark', 'pc-gemma4-e2b', 'gemma4:e2b', 240, 2048, 78, FALSE, FALSE, TRUE, 'Fast edge Gemma4 model for local drafting and smoke tests.'),
    ('litellm', 'pc-gemma4-e4b', 'PC Gemma4 E4B', 'gemma4', 'local_llm', FALSE, FALSE, TRUE, FALSE, TRUE, TRUE, TRUE, 'pending_benchmark', 'pc-gemma4-e4b', 'gemma4:e4b', 300, 2048, 80, FALSE, TRUE, TRUE, 'Primary CEO PC local Gemma4 candidate.'),
    ('litellm', 'pc-gemma4-26b', 'PC Gemma4 26B A4B Compare', 'gemma4', 'local_llm_compare', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE, TRUE, 'comparison_only', 'pc-gemma4-26b', 'gemma4:26b', 600, 2048, 90, TRUE, FALSE, FALSE, 'Comparison-only MoE route; enable after benchmark if stable.'),
    ('litellm', 'pc-gemma4-31b', 'PC Gemma4 31B Dense Compare', 'gemma4', 'local_llm_compare', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE, TRUE, 'comparison_only', 'pc-gemma4-31b', 'gemma4:31b', 600, 2048, 92, TRUE, FALSE, FALSE, 'Comparison-only dense workstation route; expected to be slower on RTX 3060.'),
    ('litellm', 'pc-qwen3-0.6b', 'PC Qwen3 0.6B Tiny', 'qwen3', 'local_llm_tiny', FALSE, FALSE, FALSE, FALSE, FALSE, TRUE, TRUE, 'utility_only', 'pc-qwen3-0.6b', 'qwen3:0.6b', 180, 1024, 110, TRUE, FALSE, FALSE, 'Tiny utility model for latency baselines only.'),
    ('litellm', 'pc-qwen3-1.7b', 'PC Qwen3 1.7B Small', 'qwen3', 'local_llm_small', FALSE, FALSE, FALSE, FALSE, FALSE, TRUE, TRUE, 'utility_only', 'pc-qwen3-1.7b', 'qwen3:1.7b', 180, 1024, 112, TRUE, FALSE, FALSE, 'Small utility model for latency baselines only.'),
    ('litellm', 'pc-qwen3-4b', 'PC Qwen3 4B Fast', 'qwen3', 'local_llm_fast', FALSE, TRUE, FALSE, TRUE, TRUE, TRUE, TRUE, 'pending_benchmark', 'pc-qwen3-4b', 'qwen3:4b', 240, 2048, 114, FALSE, FALSE, TRUE, 'Fast Qwen3 model for Korean summaries and tool-adjacent drafts.'),
    ('litellm', 'pc-qwen3-8b', 'PC Qwen3 8B General', 'qwen3', 'local_llm', FALSE, TRUE, FALSE, TRUE, TRUE, TRUE, TRUE, 'pending_benchmark', 'pc-qwen3-8b', 'qwen3:8b', 300, 2048, 116, FALSE, FALSE, TRUE, 'Balanced local Qwen3 model for general Korean/code tasks.'),
    ('litellm', 'pc-qwen3-14b', 'PC Qwen3 14B High Quality', 'qwen3', 'local_llm_quality', FALSE, TRUE, FALSE, TRUE, TRUE, TRUE, TRUE, 'pending_benchmark', 'pc-qwen3-14b', 'qwen3:14b', 420, 2048, 118, FALSE, FALSE, TRUE, 'Higher-quality local Qwen3 model; monitor VRAM pressure.'),
    ('litellm', 'pc-qwen3-30b', 'PC Qwen3 30B A3B Compare', 'qwen3', 'local_llm_compare', FALSE, TRUE, FALSE, TRUE, FALSE, TRUE, TRUE, 'comparison_only', 'pc-qwen3-30b', 'qwen3:30b', 600, 2048, 120, TRUE, FALSE, FALSE, 'Large MoE comparison route; benchmark-only on RTX 3060.'),
    ('litellm', 'pc-qwen2.5vl-3b', 'PC Qwen2.5-VL 3B Vision Fast', 'qwen2.5vl', 'local_vlm_fast', FALSE, FALSE, TRUE, FALSE, TRUE, TRUE, TRUE, 'pending_benchmark', 'pc-qwen2.5vl-3b', 'qwen2.5vl:3b', 300, 2048, 130, FALSE, FALSE, TRUE, 'Fast local vision-language model for image understanding.'),
    ('litellm', 'pc-qwen2.5vl-7b', 'PC Qwen2.5-VL 7B Vision', 'qwen2.5vl', 'local_vlm', FALSE, FALSE, TRUE, FALSE, TRUE, TRUE, TRUE, 'pending_benchmark', 'pc-qwen2.5vl-7b', 'qwen2.5vl:7b', 420, 2048, 132, FALSE, FALSE, TRUE, 'Primary local vision-language model for OCR/layout/image QA.');

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
    NULL,
    jsonb_build_object(
        'execution_backend', 'pc_ollama',
        'execution_model_id', execution_model_id,
        'canonical_model', canonical_model,
        'aliases', jsonb_build_array(canonical_model, 'pc_ollama:' || canonical_model),
        'timeout_seconds', timeout_seconds,
        'max_tokens', max_tokens,
        'routing_note', 'AADS chat routes directly to PC Agent, and PC Agent executes the model on CEO PC Ollama.'
    ),
    execution_model_id, 'manual_seed',
    NOW(), NULL, verification_status, NULL,
    jsonb_build_object('local_llm', TRUE, 'pc_ollama', TRUE, 'litellm_proxy', TRUE, 'vision', supports_vision, 'cost', '$0-runtime'),
    '{"input_per_million":0,"output_per_million":0,"currency":"USD"}'::jsonb,
    is_selectable, is_executable, NOW()
FROM _pc_ollama_seed_models
ON CONFLICT (provider, model_id)
DO UPDATE SET
    display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category,
    supports_tools = EXCLUDED.supports_tools,
    supports_thinking = EXCLUDED.supports_thinking,
    supports_vision = EXCLUDED.supports_vision,
    supports_coding = EXCLUDED.supports_coding,
    input_cost = 0,
    output_cost = 0,
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
SELECT 'llm', provider, model_id, display_order, routing_enabled, FALSE, notes, NOW(), 'migration_093'
FROM _pc_ollama_seed_models
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
SELECT provider || ':' || model_id, provider, model_id, display_order, is_hidden, is_favorite, FALSE, 'migration_093', NOW()
FROM _pc_ollama_seed_models
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
