-- 102: SaaS tenant usage/cost limits and tenant-owned usage logs.
-- Builds on migrations/100 and 101; does not relax tenant isolation guards.

BEGIN;

ALTER TABLE oauth_usage_log
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE bg_llm_usage_log
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE cost_tracking
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

UPDATE oauth_usage_log
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;
UPDATE bg_llm_usage_log
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;
UPDATE cost_tracking
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

ALTER TABLE oauth_usage_log
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id(),
    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE bg_llm_usage_log
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id(),
    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE cost_tracking
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id(),
    ALTER COLUMN tenant_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_oauth_usage_log_tenant'
           AND conrelid = 'public.oauth_usage_log'::regclass
    ) THEN
        ALTER TABLE public.oauth_usage_log
            ADD CONSTRAINT fk_oauth_usage_log_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_bg_llm_usage_log_tenant'
           AND conrelid = 'public.bg_llm_usage_log'::regclass
    ) THEN
        ALTER TABLE public.bg_llm_usage_log
            ADD CONSTRAINT fk_bg_llm_usage_log_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_cost_tracking_tenant'
           AND conrelid = 'public.cost_tracking'::regclass
    ) THEN
        ALTER TABLE public.cost_tracking
            ADD CONSTRAINT fk_cost_tracking_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_oauth_usage_tenant_month
    ON oauth_usage_log (tenant_id, created_at DESC)
    WHERE error_code IS NULL;
CREATE INDEX IF NOT EXISTS idx_bg_llm_usage_tenant_month
    ON bg_llm_usage_log (tenant_id, created_at DESC)
    WHERE success = TRUE;
CREATE INDEX IF NOT EXISTS idx_cost_tracking_tenant_month
    ON cost_tracking (tenant_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS tenant_plan_limits (
    plan_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    monthly_token_limit BIGINT NOT NULL DEFAULT 0,
    monthly_cost_limit_usd NUMERIC(12, 4) NOT NULL DEFAULT 0,
    monthly_call_limit BIGINT NOT NULL DEFAULT 0,
    soft_limit_ratio NUMERIC(5, 4) NOT NULL DEFAULT 0.8,
    hard_limit_ratio NUMERIC(5, 4) NOT NULL DEFAULT 1.0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (monthly_token_limit >= 0),
    CHECK (monthly_cost_limit_usd >= 0),
    CHECK (monthly_call_limit >= 0),
    CHECK (soft_limit_ratio >= 0 AND soft_limit_ratio <= hard_limit_ratio),
    CHECK (hard_limit_ratio >= 0)
);

INSERT INTO tenant_plan_limits
    (plan_key, name, monthly_token_limit, monthly_cost_limit_usd, monthly_call_limit, soft_limit_ratio, hard_limit_ratio)
VALUES
    ('free', 'Free', 1000000, 20.0000, 1000, 0.8, 1.0),
    ('team', 'Team', 10000000, 200.0000, 10000, 0.8, 1.0),
    ('enterprise', 'Enterprise', 100000000, 2000.0000, 100000, 0.8, 1.0),
    ('internal', 'AADS Internal', 0, 0.0000, 0, 0.9, 1.0)
ON CONFLICT (plan_key) DO UPDATE
   SET name = EXCLUDED.name,
       monthly_token_limit = EXCLUDED.monthly_token_limit,
       monthly_cost_limit_usd = EXCLUDED.monthly_cost_limit_usd,
       monthly_call_limit = EXCLUDED.monthly_call_limit,
       soft_limit_ratio = EXCLUDED.soft_limit_ratio,
       hard_limit_ratio = EXCLUDED.hard_limit_ratio,
       is_active = TRUE,
       updated_at = NOW();

CREATE TABLE IF NOT EXISTS tenant_usage_overrides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    granted_by TEXT REFERENCES saas_users(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tenant_usage_overrides_active
    ON tenant_usage_overrides (tenant_id, expires_at DESC)
    WHERE revoked_at IS NULL;

COMMIT;
