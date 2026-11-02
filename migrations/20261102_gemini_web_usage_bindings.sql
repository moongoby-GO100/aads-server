-- Additive, no browser/session metadata backfill: only authenticated connect writes.
-- Preflight: NULL means this additive registry has not been installed.
SELECT to_regclass('public.gemini_web_usage_bindings') AS bindings_before,
       to_regclass('public.gemini_web_usage_cleanup') AS cleanup_before,
       to_regclass('public.gemini_web_usage_lease') AS lease_before;
BEGIN;
CREATE TABLE IF NOT EXISTS gemini_web_usage_bindings (
    binding_key TEXT NOT NULL UNIQUE DEFAULT 'gemini-web-usage'
        CHECK (binding_key = 'gemini-web-usage'),
    tenant_id TEXT NOT NULL,
    owner_email TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    account TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE,
    endpoint JSONB NOT NULL CHECK (endpoint->>'kind' = 'local_agent'),
    sample JSONB,
    sample_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, owner_email)
);
CREATE TABLE IF NOT EXISTS gemini_web_usage_cleanup (
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_email TEXT NOT NULL,
    account TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, owner_email)
        REFERENCES gemini_web_usage_bindings(tenant_id, owner_email)
);
-- Readback: retain and expose existing row counts on repeated application.
SELECT count(*) AS bindings_after FROM gemini_web_usage_bindings;
SELECT count(*) AS cleanup_after FROM gemini_web_usage_cleanup;
-- Expired operations remain evidence: expiry does not prove PC termination.
CREATE TABLE IF NOT EXISTS gemini_web_usage_lease (
    lane TEXT PRIMARY KEY CHECK (lane='gemini-web-usage'),
    token TEXT,
    kind TEXT,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'idle'
        CHECK (state IN ('idle','running','recovery_required')),
    expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO gemini_web_usage_lease (lane) VALUES ('gemini-web-usage') ON CONFLICT DO NOTHING;
ALTER TABLE gemini_web_usage_cleanup ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE gemini_web_usage_cleanup ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE gemini_web_usage_cleanup ADD COLUMN IF NOT EXISTS last_reason TEXT;
ALTER TABLE gemini_web_usage_cleanup ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ NOT NULL DEFAULT now();
SELECT lane, kind, state, expires_at FROM gemini_web_usage_lease;
SELECT count(*) AS preserved_cleanup FROM gemini_web_usage_cleanup;
ALTER TABLE gemini_web_usage_cleanup ADD COLUMN IF NOT EXISTS work_key TEXT NOT NULL DEFAULT 'extai-fixed-profile';
COMMIT;
