-- Secure credential input: replace mode for never-verified accounts.
-- Additive only: two nullable/defaulted columns on agent_vault_credential_requests.
-- mode='replace' + target_credential_id => submit updates that existing unverified credential
-- instead of inserting a new row. No secret columns.
BEGIN;

ALTER TABLE agent_vault_credential_requests
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'create';

ALTER TABLE agent_vault_credential_requests
    ADD COLUMN IF NOT EXISTS target_credential_id uuid;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'agent_vault_credential_requests_mode_chk'
    ) THEN
        ALTER TABLE agent_vault_credential_requests
            ADD CONSTRAINT agent_vault_credential_requests_mode_chk
            CHECK (mode IN ('create', 'replace'));
    END IF;
END $$;

COMMIT;
