-- Rollback: AADS-OBYS-VAULT-SEPARATION-20260930
-- 진아서버 오비서 인증 DB 전용, 수동 적용. AADS DB 에 돌리지 않는다.
-- 주의: 이관한 오비서 자격증명(OBYS_VAULT_KEY 암호문)이 모두 사라진다.
-- AADS 원본(e2e_credentials)은 이관 스크립트가 읽기만 했으므로 그대로 있다.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.chat_sessions') IS NOT NULL THEN
        RAISE EXCEPTION '오비서 인증 DB 전용이다. AADS DB 에 적용하지 않는다';
    END IF;
END
$$;

DROP TABLE IF EXISTS public.e2e_credentials;

COMMIT;
