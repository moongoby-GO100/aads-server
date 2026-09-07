-- Promote Claude Opus 5 from hidden compatibility alias to a selectable
-- Claude CLI runner model. Safe to re-run.

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
    input_cost,
    output_cost,
    is_active,
    activation_source,
    linked_key_name,
    metadata,
    execution_model_id,
    discovery_source,
    verification_status,
    capabilities,
    pricing,
    is_selectable,
    is_executable,
    updated_at
)
VALUES (
    'anthropic',
    'claude-opus-5',
    'Claude Opus 5 (Claude CLI)',
    'claude',
    'coding',
    TRUE,
    TRUE,
    TRUE,
    TRUE,
    5.0,
    25.0,
    TRUE,
    'manual',
    NULL,
    '{
      "model_source":"template",
      "execution_backend":"claude_cli_relay",
      "execution_model_id":"claude-opus-5",
      "runtime_executable":true,
      "requires_admin_review":false,
      "routing_note":"Claude CLI Opus 5 runner model; normalized to opus for Claude Code CLI execution."
    }'::jsonb,
    'claude-opus-5',
    'manual_seed',
    'verified',
    '{"tools":true,"thinking":true,"vision":true,"coding":true,"claude_cli":true}'::jsonb,
    '{"input_cost":"5.0","output_cost":"25.0","unit":"usd_per_1m_tokens"}'::jsonb,
    TRUE,
    TRUE,
    NOW()
)
ON CONFLICT (provider, model_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category,
    supports_tools = TRUE,
    supports_thinking = TRUE,
    supports_vision = TRUE,
    supports_coding = TRUE,
    input_cost = EXCLUDED.input_cost,
    output_cost = EXCLUDED.output_cost,
    is_active = TRUE,
    activation_source = 'manual',
    metadata = (
        COALESCE(llm_models.metadata, '{}'::jsonb)
        - 'alias_of'
        - 'model_source'
    ) || EXCLUDED.metadata,
    execution_model_id = 'claude-opus-5',
    discovery_source = 'manual_seed',
    retired_at = NULL,
    verification_status = 'verified',
    capabilities = COALESCE(llm_models.capabilities, '{}'::jsonb) || EXCLUDED.capabilities,
    pricing = COALESCE(llm_models.pricing, '{}'::jsonb) || EXCLUDED.pricing,
    is_selectable = TRUE,
    is_executable = TRUE,
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
    ('llm', 'anthropic', 'claude-opus-5', 10, TRUE, FALSE, 'Claude CLI Opus 5 selectable LLM route.', NOW(), 'migration_162_promote_claude_opus5_runner_model'),
    ('runner_llm', 'anthropic', 'claude-opus-5', 10, TRUE, FALSE, 'Claude CLI Opus 5 runner fallback route.', NOW(), 'migration_162_promote_claude_opus5_runner_model')
ON CONFLICT (route_key, provider, model_id) DO UPDATE
SET is_enabled = TRUE,
    display_order = LEAST(model_routing_preferences.display_order, EXCLUDED.display_order),
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;
