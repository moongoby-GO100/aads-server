-- Browser login save slots: the temporary id/password a browser_fill saw, kept until the CEO decides the
-- "save to Agent Vault?" card. Lives in PostgreSQL so a blue/green swap or restart cannot expire a card
-- that is still inside its TTL (2026-10-06: card 29b81e73 approved 17:06:49, process had restarted 17:06:56).
-- Self-contained and idempotent: no FK, no reference to any other table, safe to run any number of times.
-- username_enc / password_enc are credential_vault.encrypt_value() ciphertext; plaintext is never stored.
BEGIN;

CREATE TABLE IF NOT EXISTS browser_login_save_slots (
    id            text PRIMARY KEY,
    tenant_id     uuid NOT NULL,
    session_id    text NOT NULL DEFAULT '',
    origin        text NOT NULL,
    login_url     text NOT NULL DEFAULT '',
    username_enc  text NOT NULL,
    password_enc  text NOT NULL,
    request_id    text,
    expires_at    timestamptz NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT browser_login_save_slots_scope_uq UNIQUE (tenant_id, session_id, origin)
);

CREATE INDEX IF NOT EXISTS browser_login_save_slots_expires_idx
    ON browser_login_save_slots (expires_at);

COMMIT;
