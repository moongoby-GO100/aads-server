-- M3 A-scope: authenticated-site account credential approval.
-- Additive only. The account retains an opaque Agent Vault reference; no
-- credential material, cookies, or login challenge values are stored here.

ALTER TABLE authenticated_site_accounts
    ADD COLUMN IF NOT EXISTS credential_approval_request_id UUID NULL
        REFERENCES agent_permission_requests(id) ON DELETE SET NULL;

ALTER TABLE authenticated_site_accounts
    ADD COLUMN IF NOT EXISTS credential_scope JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_authenticated_site_accounts_tenant_approval
    ON authenticated_site_accounts(tenant_id, credential_approval_request_id)
    WHERE credential_approval_request_id IS NOT NULL;

COMMENT ON COLUMN authenticated_site_accounts.vault_reference IS
    'Opaque Agent Vault credential id only; never credential material.';
COMMENT ON COLUMN authenticated_site_accounts.credential_scope IS
    'Secret-free A-scope approval context (origin, work key, site key).';
