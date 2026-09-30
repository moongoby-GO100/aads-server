-- AADS-OBYS-CONTRACT-TO-EMPLOYMENT-PAYROLL-20260930
-- 고용조건 스냅샷 교체·급여 계약 이탈 감사로그 조회용 인덱스.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_employment_audit_lookup.sql
-- 스키마 추가만 한다. 스냅샷(current_employment*)은 yeoljeong_employee_join_requests.request_payload,
--   급여 근거(source_contract_id·contract_defaults_applied·contract_deviation)는
--   yeoljeong_payroll_statements.statement_payload jsonb 에 들어가므로 컬럼 추가가 필요 없다.
-- 기존 행을 읽거나 바꾸지 않는다. 백필은 scripts/backfill_employment_snapshots.py(--dry-run 기본)로 따로 한다.
-- 멱등하다(IF NOT EXISTS).
-- 롤백: migrations/rollback/20260930_obys_employment_audit_lookup.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_audit_logs') IS NULL
       OR to_regclass('public.yeoljeong_business_tenant_mapping') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_audit_logs / yeoljeong_business_tenant_mapping 없음)';
    END IF;
END
$$;

-- 직원별 고용 이력(resource_type=employee_employment, resource_id=request_id)과
-- 급여명세별 계약 이탈(resource_type=payroll_statement) 조회.
CREATE INDEX IF NOT EXISTS idx_yeoljeong_audit_logs_resource
    ON yeoljeong_audit_logs (business_id, resource_type, resource_id, created_at DESC);

COMMIT;
