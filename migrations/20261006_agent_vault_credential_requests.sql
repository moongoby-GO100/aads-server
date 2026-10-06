-- Secure credential input requests: Vault has no account for a site -> CEO types it into a chat card.
-- Additive only: one new table, no secret columns (the password never touches this table).
-- Plain uuids (no FK) so tenant/session/credential cleanup can never block the request trail.
BEGIN;

CREATE TABLE IF NOT EXISTS agent_vault_credential_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    session_id text NOT NULL DEFAULT '',
    origin text NOT NULL,
    login_url text NOT NULL DEFAULT '',
    browser_work_key text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'submitted', 'verified', 'failed', 'expired', 'cancelled')),
    credential_id uuid,
    permission_request_id uuid,
    reason text NOT NULL DEFAULT '',
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '30 minutes'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- At most one live request per (tenant, session, origin): concurrent triggers reuse it.
CREATE UNIQUE INDEX IF NOT EXISTS agent_vault_credential_requests_pending_uq
    ON agent_vault_credential_requests (tenant_id, session_id, origin)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS agent_vault_credential_requests_tenant_status_idx
    ON agent_vault_credential_requests (tenant_id, status, expires_at);

CREATE INDEX IF NOT EXISTS agent_vault_credential_requests_card_idx
    ON agent_vault_credential_requests (permission_request_id)
    WHERE permission_request_id IS NOT NULL;

COMMIT;
