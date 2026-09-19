-- Rollback for 20260919_authenticated_site_account_approval_scope.sql.
-- Apply only after confirming no account depends on the recorded approval
-- reference. This removes additive M3 metadata only; Agent Vault credentials,
-- permission requests, and audit evidence are intentionally preserved.

DROP INDEX IF EXISTS idx_authenticated_site_accounts_tenant_approval;
ALTER TABLE authenticated_site_accounts
    DROP CONSTRAINT IF EXISTS fk_authenticated_site_accounts_tenant_approval,
    DROP COLUMN IF EXISTS credential_scope,
    DROP COLUMN IF EXISTS credential_approval_request_id;
DROP INDEX IF EXISTS uq_agent_permission_requests_tenant_id_id;
