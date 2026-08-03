-- OHVIS app push notification subscriptions.
CREATE TABLE IF NOT EXISTS app_push_subscriptions (
    id UUID PRIMARY KEY,
    tenant_id UUID NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    subscription JSONB NOT NULL,
    user_agent TEXT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_success_at TIMESTAMPTZ NULL,
    last_error TEXT NULL,
    last_error_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE NULLS NOT DISTINCT (tenant_id, user_id, endpoint)
);

ALTER TABLE app_push_subscriptions
    DROP CONSTRAINT IF EXISTS app_push_subscriptions_tenant_id_user_id_endpoint_key;

ALTER TABLE app_push_subscriptions
    ADD CONSTRAINT app_push_subscriptions_tenant_id_user_id_endpoint_key
    UNIQUE NULLS NOT DISTINCT (tenant_id, user_id, endpoint);

CREATE INDEX IF NOT EXISTS idx_app_push_subscriptions_user_enabled
    ON app_push_subscriptions(tenant_id, user_id, enabled);
