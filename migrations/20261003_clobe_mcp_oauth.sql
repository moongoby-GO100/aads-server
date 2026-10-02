-- 클로브AI MCP OAuth 연결(읽기 전용). DDL 정본은 app/services/clobe_mcp_client.py SCHEMA_DDL (런타임 ensure_schema 와 동일).
-- 롤백: migrations/rollback/20261003_clobe_mcp_oauth.down.sql
BEGIN;
CREATE TABLE IF NOT EXISTS clobe_mcp_oauth_client (
    redirect_uri TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS clobe_mcp_oauth_state (
    state_hash        TEXT PRIMARY KEY,
    code_verifier_enc TEXT NOT NULL,
    redirect_uri      TEXT NOT NULL,
    created_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    consumed_at       TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS clobe_mcp_connection (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    client_id        TEXT,
    access_token_enc TEXT,
    refresh_token_enc TEXT,
    token_expires_at TIMESTAMPTZ,
    scope            TEXT,
    connected_at     TIMESTAMPTZ,
    last_success_at  TIMESTAMPTZ,
    last_error       TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS clobe_mcp_tools (
    name         TEXT PRIMARY KEY,
    description  TEXT,
    input_schema JSONB,
    annotations  JSONB,
    allowed      BOOLEAN NOT NULL DEFAULT FALSE,
    deny_reason  TEXT,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMIT;
