-- Rollback: AADS-OBYS-EMPLOYEE-TENANT-MEMBERSHIP-20260930
-- 오비서 업무 DB 전용, 수동 적용. AADS DB 에 돌리지 않는다.
-- 주의: 멤버십 연결·회수 감사 행(ledger_table='tenant_memberships')이 사라진다.
--   필요하면 먼저 내보낸다:
--   \copy (SELECT * FROM yeoljeong_hr_tenant_attribution_audit WHERE ledger_table='tenant_memberships') TO 'membership_audit.csv' CSV HEADER
-- tenant_memberships(AADS 인증 DB) 자체는 건드리지 않는다.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_hr_tenant_attribution_audit') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다';
    END IF;
END
$$;

DELETE FROM yeoljeong_hr_tenant_attribution_audit WHERE ledger_table = 'tenant_memberships';
DROP INDEX IF EXISTS idx_yf_hr_attr_audit_membership;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit
    DROP CONSTRAINT IF EXISTS yeoljeong_hr_tenant_attribution_audit_classification_check;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit
    ADD CONSTRAINT yeoljeong_hr_tenant_attribution_audit_classification_check
    CHECK (classification IN ('attributed', 'unresolved', 'conflict'));
ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP COLUMN IF EXISTS action;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP COLUMN IF EXISTS actor_user_id;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP COLUMN IF EXISTS actor_email;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP COLUMN IF EXISTS employee_email;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP COLUMN IF EXISTS employee_user_id;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP COLUMN IF EXISTS join_request_id;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP COLUMN IF EXISTS before_state;
ALTER TABLE yeoljeong_hr_tenant_attribution_audit DROP COLUMN IF EXISTS after_state;

COMMIT;
