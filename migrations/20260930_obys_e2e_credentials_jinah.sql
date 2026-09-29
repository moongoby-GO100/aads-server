-- 오비서 전용 비밀번호관리자 — 진아서버 인증 DB 에 e2e_credentials 생성
--
-- 배경 (2026-09-30 실측):
--   진아서버 오비서 인증 DB(OBYS_AUTH_DATABASE_URL)에는 인증 테이블 7개뿐이라
--   app/api/auth.py 의 /auth/login/e2e-inject(credential_vault.get_credential)가
--   동작하지 않는다. AADS 와 같은 스키마로 테이블만 만든다.
--   암호문은 AADS VAULT_ENCRYPTION_KEY 가 아니라 오비서 전용 OBYS_VAULT_KEY 로
--   만든다(scripts/obys_vault_migrate.py). 두 키는 달라야 한다.
--
-- 스키마 원본: migrations/046_e2e_credential_vault.sql + 101_saas_tenant_isolation_guards.sql
-- 적용 대상: 진아서버 인증 DB 만. AADS DB 에 돌리지 않는다.
-- 선행: 20260923_obys_auth_bootstrap_jinah.sql (aads_internal_tenant_id 함수).
-- 멱등하며 기존 행을 바꾸지 않는다.
-- 롤백: migrations/rollback/20260930_obys_e2e_credentials_jinah.down.sql

BEGIN;

DO $$
BEGIN
    -- AADS DB 오적용 차단. 오비서 인증 DB 에는 chat_sessions 가 없다.
    -- (apply_release_migrations.sh 는 baseline 목록으로 이 파일을 HOLD 한다.)
    IF to_regclass('public.chat_sessions') IS NOT NULL THEN
        RAISE EXCEPTION '오비서 인증 DB 전용이다. AADS DB 에 적용하지 않는다';
    END IF;
    IF to_regclass('public.tenants') IS NULL
       OR to_regprocedure('public.aads_internal_tenant_id()') IS NULL THEN
        RAISE EXCEPTION
            '인증 DB 부트스트랩이 먼저다: 20260923_obys_auth_bootstrap_jinah.sql';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.e2e_credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service         VARCHAR(100) NOT NULL,
    project         VARCHAR(20),
    label           VARCHAR(100) NOT NULL DEFAULT '기본',
    login_url       TEXT,
    username_enc    TEXT NOT NULL,                  -- OBYS_VAULT_KEY Fernet 암호문
    password_enc    TEXT NOT NULL,                  -- OBYS_VAULT_KEY Fernet 암호문
    extra_fields    JSONB DEFAULT '{}',
    login_steps     JSONB DEFAULT '[]',
    is_active       BOOLEAN DEFAULT TRUE,
    last_used_at    TIMESTAMPTZ,
    last_verified   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    tenant_id       UUID NOT NULL DEFAULT public.aads_internal_tenant_id(),
    CONSTRAINT fk_e2e_credentials_tenant
        FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_cred_tenant_service_project_label
    ON public.e2e_credentials (tenant_id, service, COALESCE(project, '_ALL_'), label);
CREATE INDEX IF NOT EXISTS idx_e2e_cred_tenant_project
    ON public.e2e_credentials (tenant_id, project)
    WHERE is_active = TRUE;

COMMENT ON TABLE public.e2e_credentials IS
    '오비서 자격증명 저장소 — OBYS_VAULT_KEY Fernet 암호화 (AADS 키와 분리)';

-- 검증: tenant_id 가 필수인지 확인한다. 예전 스키마로 이미 있던 테이블이면 여기서 멈춘다.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'e2e_credentials'
           AND column_name = 'tenant_id' AND is_nullable = 'YES'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'e2e_credentials'
           AND column_name = 'tenant_id'
    ) THEN
        RAISE EXCEPTION 'e2e_credentials.tenant_id 가 NOT NULL 이 아니다';
    END IF;
END
$$;

COMMIT;
