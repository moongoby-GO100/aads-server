-- 154: Register GPT-6 Astra as a Codex CLI selectable model.
-- Official Codex docs list `codex -m gpt-6-astra`; this keeps AADS runner/chat
-- model pickers aligned without changing CEO-configured runner size order.

INSERT INTO llm_models (
    provider, model_id, display_name, family, category,
    input_cost, output_cost,
    is_active, is_selectable, is_executable,
    activation_source, execution_model_id, discovery_source, verification_status,
    supports_tools, supports_thinking, supports_vision, supports_coding,
    metadata, capabilities, pricing, created_at, updated_at
)
VALUES (
    'codex', 'gpt-6-astra', 'GPT-6 Astra (Codex CLI)', 'codex', 'coding',
    10.0, 50.0,
    TRUE, TRUE, TRUE,
    'manual', 'gpt-6-astra', 'official_codex_docs', 'verified',
    TRUE, TRUE, TRUE, TRUE,
    '{"execution_backend":"codex_cli","execution_model_id":"gpt-6-astra","routing_note":"Official Codex CLI model; use via codex -m gpt-6-astra. Does not require OpenAI API billing in ChatGPT OAuth Codex relay."}'::jsonb,
    '{"tools":true,"thinking":true,"vision":true,"coding":true,"computer_use":true,"file_search":true,"web_search":true,"codex_cli":true,"chatgpt_credits":true}'::jsonb,
    '{"input_cost":"10.0","output_cost":"50.0","unit":"usd_per_1m_tokens","source":"official_docs_or_manual_seed"}'::jsonb,
    NOW(), NOW()
)
ON CONFLICT (provider, model_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category,
    input_cost = EXCLUDED.input_cost,
    output_cost = EXCLUDED.output_cost,
    is_active = TRUE,
    is_selectable = TRUE,
    is_executable = TRUE,
    activation_source = 'manual',
    execution_model_id = EXCLUDED.execution_model_id,
    discovery_source = EXCLUDED.discovery_source,
    verification_status = 'verified',
    supports_tools = TRUE,
    supports_thinking = TRUE,
    supports_vision = TRUE,
    supports_coding = TRUE,
    metadata = COALESCE(llm_models.metadata, '{}'::jsonb) || EXCLUDED.metadata,
    capabilities = COALESCE(llm_models.capabilities, '{}'::jsonb) || EXCLUDED.capabilities,
    pricing = COALESCE(llm_models.pricing, '{}'::jsonb) || EXCLUDED.pricing,
    updated_at = NOW();

INSERT INTO model_routing_preferences (
    route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by
)
VALUES
    ('llm', 'codex', 'gpt-6-astra', 8, TRUE, FALSE, 'Official Codex CLI Astra model; selectable but not default until runtime relay is smoke-tested.', NOW(), 'migration_154_codex_cli_gpt6_astra'),
    ('runner_llm', 'codex', 'gpt-6-astra', 8, TRUE, FALSE, 'Official Codex CLI Astra model for runner selection.', NOW(), 'migration_154_codex_cli_gpt6_astra'),
    ('code_exec', 'codex', 'gpt-6-astra', 8, TRUE, FALSE, 'Official Codex CLI Astra model for code execution selection.', NOW(), 'migration_154_codex_cli_gpt6_astra')
ON CONFLICT (route_key, provider, model_id) DO UPDATE
SET display_order = EXCLUDED.display_order,
    is_enabled = TRUE,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;
