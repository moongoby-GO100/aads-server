-- 049: saas_users id 타입 통일(TEXT) + E2E CEO 계정 보정
-- NOTE:
--   - CEO 계정 시드는 DB GUC에서 비밀번호를 읽습니다.
--   - 예: PGOPTIONS="-c app.aads_admin_password=$AADS_ADMIN_PASSWORD -c app.aads_admin_email=$AADS_ADMIN_EMAIL"

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
DECLARE
    id_data_type TEXT;
BEGIN
    IF to_regclass('public.saas_users') IS NULL THEN
        RAISE NOTICE 'saas_users table not found; skip migration 049';
        RETURN;
    END IF;

    -- E2E 쿼리 호환 컬럼 보강
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'saas_users'
          AND column_name = 'username'
    ) THEN
        ALTER TABLE public.saas_users ADD COLUMN username TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'saas_users'
          AND column_name = 'role'
    ) THEN
        ALTER TABLE public.saas_users ADD COLUMN role TEXT NOT NULL DEFAULT 'user';
    END IF;

    -- id 타입 통일: SERIAL/UUID/기타 -> TEXT
    SELECT c.data_type
      INTO id_data_type
      FROM information_schema.columns c
     WHERE c.table_schema = 'public'
       AND c.table_name = 'saas_users'
       AND c.column_name = 'id';

    IF id_data_type IS DISTINCT FROM 'text' THEN
        ALTER TABLE public.saas_users ALTER COLUMN id DROP DEFAULT;
        ALTER TABLE public.saas_users
            ALTER COLUMN id TYPE TEXT USING id::TEXT;
    END IF;

    ALTER TABLE public.saas_users
        ALTER COLUMN id SET DEFAULT gen_random_uuid()::TEXT;
END $$;

-- 기존 데이터 정리(데이터 손실 없음)
UPDATE public.saas_users
   SET role = 'user'
 WHERE role IS NULL
    OR btrim(role) = '';

UPDATE public.saas_users
   SET username = split_part(email, '@', 1)
 WHERE (username IS NULL OR btrim(username) = '')
   AND email IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_saas_users_username ON public.saas_users(username);
CREATE INDEX IF NOT EXISTS idx_saas_users_role ON public.saas_users(role);

-- E2E 테스트 계정 보정: username='admin' 또는 role='ceo'가 없으면 생성
DO $$
DECLARE
    v_admin_email TEXT := COALESCE(
        NULLIF(current_setting('app.aads_admin_email', true), ''),
        NULLIF(current_setting('aads.admin_email', true), ''),
        'admin@aads.dev'
    );
    v_admin_password TEXT := COALESCE(
        NULLIF(current_setting('app.aads_admin_password', true), ''),
        NULLIF(current_setting('aads.admin_password', true), '')
    );
    v_exists BOOLEAN;
BEGIN
    IF to_regclass('public.saas_users') IS NULL THEN
        RETURN;
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM public.saas_users
         WHERE username = 'admin'
            OR role = 'ceo'
            OR email = v_admin_email
    ) INTO v_exists;

    IF v_exists THEN
        RAISE NOTICE 'CEO/admin user already exists; skip seed';
        RETURN;
    END IF;

    IF v_admin_password IS NULL THEN
        RAISE NOTICE 'AADS admin password is not provided via DB setting; skip CEO seed';
        RETURN;
    END IF;

    INSERT INTO public.saas_users (
        id,
        email,
        password_hash,
        name,
        username,
        role,
        created_at,
        updated_at
    )
    VALUES (
        gen_random_uuid()::TEXT,
        v_admin_email,
        crypt(v_admin_password, gen_salt('bf')),
        'CEO Admin',
        'admin',
        'ceo',
        NOW(),
        NOW()
    );
END $$;
