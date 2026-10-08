-- 카페24 오비서(OBYS DB)의 클로브 MCP 토큰 저장소 + 앱 역할 최소 권한. 롤백: migrations/rollback/20261008_obys_clobe_mcp_store.down.sql
-- 적용 대상은 acct-pg 의 obys DB (AADS DB 아님). 멱등. DDL 정본은 app/services/clobe_mcp_client.py SCHEMA_DDL.
-- contabo116 의 토큰·암호화 키·oauth_client 값은 옮기지 않는다 — 테이블은 비어 있는 채로 생기고 카페24 에서 새로 OAuth 한다.
-- 권한은 앱 런타임 역할 하나에만 준다. 삭제·TRUNCATE·DDL 권한은 주지 않는다(앱 코드가 쓰지 않는다).
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

-- 앱 런타임 역할. 역할이 없는 DB(개발·검증)에서는 건너뛴다. 역할 이름을 바꾸면(r5 -> r6) 이 파일이 아니라
-- 새 마이그레이션으로 같은 GRANT 를 준다.
DO $grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acct_business_runtime_r5') THEN
        -- 수집 계약(20261003_obys_clobe_collection.sql): 코드가 읽고 쓰는 만큼만 (ON CONFLICT DO UPDATE 포함).
        GRANT SELECT, INSERT, UPDATE ON
            public.obys_clobe_company_link,
            public.obys_clobe_collection_lease,
            public.obys_clobe_collection_run,
            public.obys_clobe_collection_state,
            public.obys_clobe_item,
            public.obys_clobe_ledger_entry
        TO acct_business_runtime_r5;
        -- 토큰 저장소. oauth_client 는 등록 후 읽기만 하고(DO NOTHING 삽입), 나머지는 상태 갱신이 있다.
        GRANT SELECT, INSERT ON public.clobe_mcp_oauth_client TO acct_business_runtime_r5;
        GRANT SELECT, INSERT, UPDATE ON
            public.clobe_mcp_oauth_state,
            public.clobe_mcp_connection,
            public.clobe_mcp_tools
        TO acct_business_runtime_r5;
    ELSE
        RAISE NOTICE 'role acct_business_runtime_r5 not found; grants skipped';
    END IF;
END
$grant$;

COMMIT;
