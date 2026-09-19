-- Rollback for 20260919_authenticated_site_account_approval_scope.sql.
-- Apply only after confirming no account depends on the recorded approval
-- reference. This removes additive M3 metadata only; Agent Vault credentials,
-- permission requests, and audit evidence are intentionally preserved.

DROP INDEX IF EXISTS idx_authenticated_site_accounts_tenant_approval;
ALTER TABLE authenticated_site_accounts
    DROP COLUMN IF EXISTS credential_scope,
    DROP COLUMN IF EXISTS credential_approval_request_id;
