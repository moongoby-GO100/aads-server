-- M3 A-scope: authenticated-site account credential approval.
-- Additive only. The account retains an opaque Agent Vault reference; no
-- credential material, cookies, or login challenge values are stored here.

ALTER TABLE authenticated_site_accounts
    ADD COLUMN IF NOT EXISTS credential_approval_request_id UUID NULL;

ALTER TABLE authenticated_site_accounts
    ADD COLUMN IF NOT EXISTS credential_scope JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_permission_requests_tenant_id_id
    ON agent_permission_requests(tenant_id, id);

DO $$
DECLARE
    legacy_constraint TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_authenticated_site_accounts_tenant_approval'
    ) THEN
        ALTER TABLE authenticated_site_accounts
            ADD CONSTRAINT fk_authenticated_site_accounts_tenant_approval
            FOREIGN KEY (tenant_id, credential_approval_request_id)
            REFERENCES agent_permission_requests(tenant_id, id)
            ON DELETE SET NULL (credential_approval_request_id);
    END IF;

    -- The original additive column declaration used a single-column FK.  It
    -- must not coexist with the composite FK: either FK satisfying an insert
    -- would otherwise permit an approval request owned by another tenant.
    FOR legacy_constraint IN
        SELECT c.conname
          FROM pg_constraint c
         WHERE c.conrelid = 'authenticated_site_accounts'::regclass
           AND c.contype = 'f'
           AND c.confrelid = 'agent_permission_requests'::regclass
           AND c.conkey = ARRAY[
               (SELECT attnum
                  FROM pg_attribute
                 WHERE attrelid = 'authenticated_site_accounts'::regclass
                   AND attname = 'credential_approval_request_id'
                   AND NOT attisdropped)
           ]
    LOOP
        EXECUTE format(
            'ALTER TABLE authenticated_site_accounts DROP CONSTRAINT %I',
            legacy_constraint
        );
    END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_authenticated_site_accounts_tenant_approval
    ON authenticated_site_accounts(tenant_id, credential_approval_request_id)
    WHERE credential_approval_request_id IS NOT NULL;

COMMENT ON COLUMN authenticated_site_accounts.vault_reference IS
    'Opaque Agent Vault credential id only; never credential material.';
COMMENT ON COLUMN authenticated_site_accounts.credential_scope IS
    'Secret-free A-scope approval context (origin, work key, site key).';
