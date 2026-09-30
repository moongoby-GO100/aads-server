-- AADS-OBYS-EMPLOYEE-TENANT-MEMBERSHIP-20260930
-- 직원 가입요청 승인·반려 시 고용주 테넌트 멤버십 연결·회수 감사.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_employee_membership_audit.sql
-- 기존 감사 테이블(yeoljeong_hr_tenant_attribution_audit, 20260919 O2)에 컬럼만 더하고
--   classification CHECK 에 membership_* 값을 추가한다. 기존 행은 다시 쓰지 않는다.
--   멤버십 이벤트는 ledger_table='tenant_memberships', row_id=<이벤트 uuid> 로 쌓인다
--   (PK (ledger_table,row_id) 를 그대로 쓰므로 O2 분류 행과 섞이지 않는다).
-- 멱등하다(IF NOT EXISTS, 제약 재생성).
-- 롤백: migrations/rollback/20260930_obys_employee_membership_audit.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_hr_tenant_attribution_audit') IS NULL
       OR to_regclass('public.yeoljeong_employee_join_requests') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_hr_tenant_attribution_audit / yeoljeong_employee_join_requests 없음)';
    END IF;
END
$$;

ALTER TABLE yeoljeong_hr_tenant_attribution_audit ADD COLUMN IF NOT EXISTS action TEXT;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit ADD COLUMN IF NOT EXISTS actor_user_id TEXT;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit ADD COLUMN IF NOT EXISTS actor_email TEXT;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit ADD COLUMN IF NOT EXISTS employee_email TEXT;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit ADD COLUMN IF NOT EXISTS employee_user_id TEXT;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit ADD COLUMN IF NOT EXISTS join_request_id TEXT;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit ADD COLUMN IF NOT EXISTS before_state JSONB;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit ADD COLUMN IF NOT EXISTS after_state JSONB;

-- 20260919 에서 인라인으로 만든 CHECK 는 이름이 자동 부여됐다. 이름에 기대지 않고 찾아 지운다.
DO $$
DECLARE
    c record;
BEGIN
    FOR c IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'public.yeoljeong_hr_tenant_attribution_audit'::regclass
           AND contype = 'c'
           AND pg_get_constraintdef(oid) LIKE '%classification%'
    LOOP
        EXECUTE format('ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP CONSTRAINT %I', c.conname);
    END LOOP;
END
$$;

ALTER TABLE yeoljeong_hr_tenant_attribution_audit
    ADD CONSTRAINT yeoljeong_hr_tenant_attribution_audit_classification_check
    CHECK (classification IN (
        'attributed', 'unresolved', 'conflict',
        'membership_linked', 'membership_unchanged', 'membership_pending',
        'membership_removed', 'membership_skipped', 'membership_failed'
    ));

CREATE INDEX IF NOT EXISTS idx_yf_hr_attr_audit_membership
    ON yeoljeong_hr_tenant_attribution_audit (tenant_id, employee_email, classified_at DESC)
    WHERE ledger_table = 'tenant_memberships';

COMMIT;
