CREATE TABLE IF NOT EXISTS feature_flags (
    flag_key VARCHAR(100) PRIMARY KEY,
    enabled BOOLEAN DEFAULT true,
    scope VARCHAR(20) DEFAULT 'global',
    last_changed_by VARCHAR(100),
    last_changed_at TIMESTAMPTZ DEFAULT NOW(),
    notes TEXT
);

INSERT INTO feature_flags (flag_key, enabled) VALUES
    ('governance_enabled', true),
    ('intent_policies_db_primary', false),
    ('prompt_variants_enabled', false),
    ('tool_grants_enforced', false)
ON CONFLICT (flag_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS governance_audit_log (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ DEFAULT NOW(),
    event VARCHAR(50) NOT NULL,
    mode VARCHAR(20) NOT NULL,
    legacy_result JSONB,
    db_result JSONB,
    diff_summary TEXT,
    trace_id VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_gal_at ON governance_audit_log(at DESC);
CREATE INDEX IF NOT EXISTS idx_gal_event ON governance_audit_log(event);
