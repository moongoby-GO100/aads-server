-- Session Governance Architecture v2.1 Phase 1-A
-- Governance foundational tables + minimal seed data

CREATE TABLE IF NOT EXISTS governance_events (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    actor VARCHAR(64),
    subject VARCHAR(128),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_governance_events_event_type
    ON governance_events(event_type);

CREATE INDEX IF NOT EXISTS idx_governance_events_created_at
    ON governance_events(created_at DESC);

CREATE TABLE IF NOT EXISTS intent_policies (
    id BIGSERIAL PRIMARY KEY,
    intent VARCHAR(64) NOT NULL UNIQUE,
    allowed_models TEXT[] NOT NULL,
    default_model VARCHAR(64) NOT NULL,
    cascade_downgrade BOOLEAN NOT NULL DEFAULT FALSE,
    tool_allowlist TEXT[],
    description TEXT,
    updated_by VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS role_profiles (
    id BIGSERIAL PRIMARY KEY,
    role VARCHAR(64) NOT NULL UNIQUE,
    system_prompt_ref VARCHAR(256),
    tool_allowlist TEXT[],
    max_turns INT DEFAULT 100,
    budget_usd NUMERIC(10, 2) DEFAULT 50,
    escalation_rules JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS change_requests (
    id BIGSERIAL PRIMARY KEY,
    target_type VARCHAR(64),
    target_id VARCHAR(128),
    diff JSONB NOT NULL,
    proposed_by VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'reverted')),
    approved_by VARCHAR(64),
    approved_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_change_requests_status
    ON change_requests(status);

CREATE INDEX IF NOT EXISTS idx_change_requests_target_type
    ON change_requests(target_type);

INSERT INTO intent_policies (
    intent,
    allowed_models,
    default_model,
    cascade_downgrade,
    tool_allowlist,
    description,
    updated_by
) VALUES
    (
        'greeting',
        ARRAY['claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'litellm:gemini-2.5-flash'],
        'claude-sonnet-4-6',
        TRUE,
        NULL,
        'Greeting and short acknowledgment responses.',
        'governance_v2_1_migration'
    ),
    (
        'casual',
        ARRAY['claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'litellm:gemini-2.5-flash'],
        'claude-sonnet-4-6',
        TRUE,
        NULL,
        'Low-risk casual conversation and lightweight queries.',
        'governance_v2_1_migration'
    ),
    (
        'status_check',
        ARRAY['claude-sonnet-4-6', 'litellm:gemini-2.5-flash'],
        'claude-sonnet-4-6',
        FALSE,
        NULL,
        'Operational status, checks, and reporting flows.',
        'governance_v2_1_migration'
    ),
    (
        'task_query',
        ARRAY['claude-sonnet-4-6', 'litellm:gemini-2.5-flash'],
        'claude-sonnet-4-6',
        FALSE,
        NULL,
        'Task progress lookup and execution state queries.',
        'governance_v2_1_migration'
    ),
    (
        'dashboard',
        ARRAY['claude-sonnet-4-6', 'claude-opus-4-6', 'litellm:gemini-2.5-flash'],
        'claude-sonnet-4-6',
        FALSE,
        NULL,
        'Dashboard summaries, management views, and admin rollups.',
        'governance_v2_1_migration'
    ),
    (
        'code_modify',
        ARRAY['claude-sonnet-4-6', 'claude-opus-4-6', 'litellm:gemini-2.5-flash'],
        'claude-sonnet-4-6',
        FALSE,
        NULL,
        'Code modification and implementation work with governed routing.',
        'governance_v2_1_migration'
    ),
    (
        'report',
        ARRAY['claude-sonnet-4-6', 'claude-opus-4-6', 'litellm:gemini-2.5-flash'],
        'claude-sonnet-4-6',
        FALSE,
        NULL,
        'Structured reporting and documentation output.',
        'governance_v2_1_migration'
    )
ON CONFLICT (intent) DO NOTHING;

INSERT INTO role_profiles (
    role,
    system_prompt_ref,
    tool_allowlist,
    max_turns,
    budget_usd,
    escalation_rules
) VALUES
    (
        'CEO',
        'app/core/prompts/system_prompt_v2.py',
        NULL,
        200,
        200.00,
        '{"approval_scope":"global","escalate_to":null}'::jsonb
    ),
    (
        'PM',
        'app/agents/pm.py',
        NULL,
        150,
        100.00,
        '{"approval_scope":"project","escalate_to":"CEO"}'::jsonb
    ),
    (
        'Developer',
        'app/agents/developer.py',
        NULL,
        120,
        75.00,
        '{"approval_scope":"code","escalate_to":"PM"}'::jsonb
    ),
    (
        'QA',
        'app/agents/qa.py',
        NULL,
        120,
        60.00,
        '{"approval_scope":"verification","escalate_to":"PM"}'::jsonb
    ),
    (
        'Ops',
        'app/agents/devops.py',
        NULL,
        100,
        80.00,
        '{"approval_scope":"operations","escalate_to":"CEO"}'::jsonb
    )
ON CONFLICT (role) DO NOTHING;
