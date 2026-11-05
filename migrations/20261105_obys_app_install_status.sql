-- 업무 DB obys 전용. AADS DB에는 적용하지 않는다.
-- 기존 obys 테이블은 행 수준 보안(RLS) 없이 tenant_id 조건과 런타임 역할 권한으로 격리한다. 같은 관례를 따른다.
-- 원본 UA 문자열·IP·위치는 저장하지 않는다. 브라우저 계열(user_agent_family)만 저장한다.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'obys' THEN
        RAISE EXCEPTION 'OBYS business database required';
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS public.obys_app_install_status (
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id),
    user_email TEXT NOT NULL CHECK (user_email = lower(user_email)),
    platform TEXT NOT NULL DEFAULT 'other' CHECK (platform IN ('ios','android','desktop','other')),
    display_mode TEXT NOT NULL DEFAULT 'browser' CHECK (display_mode IN ('standalone','browser')),
    first_standalone_at TIMESTAMPTZ,
    last_standalone_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    appinstalled_at TIMESTAMPTZ,
    user_agent_family TEXT NOT NULL DEFAULT 'other' CHECK (length(user_agent_family) <= 32),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (business_id, user_email)
);
CREATE INDEX IF NOT EXISTS idx_obys_app_install_tenant
    ON public.obys_app_install_status(tenant_id, business_id);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='acct_business_runtime_r5') THEN
        GRANT SELECT,INSERT,UPDATE ON public.obys_app_install_status TO acct_business_runtime_r5;
    END IF;
END $$;
COMMIT;
