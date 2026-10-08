-- Down migration for migrations/20261008_obys_clobe_mcp_store.sql (수동, 소유자 역할로. 허용목록 밖).
-- 권한만 회수한다. clobe_mcp_* 테이블은 카페24 에서 새로 받은 토큰을 담고 있을 수 있어 지우지 않는다.
-- 테이블까지 지우려면 CEO 확인 후 따로 한다(암호문이므로 복구 불가).
BEGIN;
DO $revoke$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acct_business_runtime_r5') THEN
        REVOKE ALL ON
            public.obys_clobe_company_link,
            public.obys_clobe_collection_lease,
            public.obys_clobe_collection_run,
            public.obys_clobe_collection_state,
            public.obys_clobe_item,
            public.obys_clobe_ledger_entry,
            public.clobe_mcp_oauth_client,
            public.clobe_mcp_oauth_state,
            public.clobe_mcp_connection,
            public.clobe_mcp_tools
        FROM acct_business_runtime_r5;
    END IF;
END
$revoke$;
COMMIT;
