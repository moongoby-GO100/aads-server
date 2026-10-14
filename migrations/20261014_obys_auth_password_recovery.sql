-- 오비서 로그인 화면: 아이디 찾기·비밀번호 재설정(관리자 임시 비밀번호 / 선택적 문자 인증).
-- 롤백: migrations/rollback/20261014_obys_auth_password_recovery.down.sql
-- 적용 대상은 acct-pg 의 obys_auth DB (인증 DB). obys(업무) DB 가 아니다. 멱등·가산 전용.
--
-- 평문 비밀번호·인증번호·연락처·이메일은 어느 컬럼에도 저장하지 않는다.
--   temp_password_hash / sms_code_hash 는 bcrypt, requested_ip_hash 는 HMAC 요약이다.
-- 임시 비밀번호는 saas_users.password_hash 를 건드리지 않는 별도 자격이다.
--   사용(로그인 성공) 시 status='used' 로 한 번만 소진되고, 그 순간 must_change_password=true 가 켜진다.
BEGIN;

ALTER TABLE public.saas_users ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false;
ALTER TABLE public.saas_users ADD COLUMN IF NOT EXISTS session_version integer NOT NULL DEFAULT 0;
ALTER TABLE public.saas_users ADD COLUMN IF NOT EXISTS password_changed_at timestamptz;

CREATE TABLE IF NOT EXISTS public.obys_password_reset_requests (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL,
    user_id             text NOT NULL REFERENCES public.saas_users(id) ON DELETE CASCADE,
    business_id         text NOT NULL DEFAULT '',
    method              text NOT NULL DEFAULT 'admin' CHECK (method IN ('admin', 'sms')),
    status              text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'issued', 'used', 'cancelled', 'completed')),
    requested_at        timestamptz NOT NULL DEFAULT now(),
    requested_ip_hash   text NOT NULL DEFAULT '',
    issued_by           text,
    issued_at           timestamptz,
    temp_password_hash  text,
    temp_expires_at     timestamptz,
    temp_used_at        timestamptz,
    sms_code_hash       text,
    sms_code_expires_at timestamptz,
    sms_failed_attempts integer NOT NULL DEFAULT 0,
    completed_at        timestamptz,
    cancelled_at        timestamptz,
    cancelled_by        text,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_obys_pwreset_tenant_status
    ON public.obys_password_reset_requests (tenant_id, status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_obys_pwreset_user_status
    ON public.obys_password_reset_requests (user_id, status);
-- 같은 계정·같은 조직·같은 방식의 열린 요청은 하나만 둔다(중복 요청은 requested_at 만 갱신).
CREATE UNIQUE INDEX IF NOT EXISTS uq_obys_pwreset_open_per_user_tenant_method
    ON public.obys_password_reset_requests (user_id, tenant_id, method)
    WHERE status IN ('pending', 'issued');

CREATE TABLE IF NOT EXISTS public.obys_password_reset_audit (
    id          bigserial PRIMARY KEY,
    request_id  uuid,
    tenant_id   uuid,
    user_id     text,
    event       text NOT NULL,
    actor       text,
    ip_hash     text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_obys_pwreset_audit_request
    ON public.obys_password_reset_audit (request_id, created_at);

-- 앱 런타임 역할을 이름으로 가정하지 않는다: saas_users 를 UPDATE 할 수 있는 비소유 역할에 같은 수준만 준다.
-- 삭제·TRUNCATE·DDL 권한은 주지 않는다.
DO $grants$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT DISTINCT g.grantee
          FROM information_schema.role_table_grants g
         WHERE g.table_schema = 'public'
           AND g.table_name = 'saas_users'
           AND g.privilege_type = 'UPDATE'
           AND g.grantee <> 'PUBLIC'
           AND g.grantee <> current_user
    LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON public.obys_password_reset_requests TO %I', r.grantee);
        EXECUTE format('GRANT SELECT, INSERT ON public.obys_password_reset_audit TO %I', r.grantee);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE public.obys_password_reset_audit_id_seq TO %I', r.grantee);
        EXECUTE format(
            'GRANT SELECT (must_change_password, session_version, password_changed_at), '
            'UPDATE (must_change_password, session_version, password_changed_at) '
            'ON public.saas_users TO %I', r.grantee);
    END LOOP;
END
$grants$;

COMMIT;
