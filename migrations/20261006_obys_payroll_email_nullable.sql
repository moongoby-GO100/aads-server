-- 급여내역서 employee_email 을 NULL 허용으로 바꾼다.
-- 급여시트 직원 대부분은 이메일이 없다. 이름만으로 먼저 저장해 두고, 초대 수락 시 계정(이메일)에 연결한다.
-- 이메일 없음의 표현은 NULL 하나다('' 금지) — CHECK 가 강제한다.
-- 멱등: 다시 실행해도 같은 상태. 소프트삭제 행도 정규화한다(VALIDATE 가 전체 행을 검사하므로).
-- 가드: aads DB 에는 yeoljeong_* 테이블이 없다(115_yeoljeong_finance_hr_ledgers.sql 미적용).
--       테이블이 없으면 아무것도 하지 않고 통과한다 — 아니면 schema_migrations 단계에서
--       모든 릴리스가 멈춘다. 테이블이 있는 DB 에서는 가드 없던 때와 같은 결과다.
BEGIN;

DO $mig$
BEGIN
    IF to_regclass('public.yeoljeong_payroll_statements') IS NULL THEN
        RAISE NOTICE 'skip 20261006_obys_payroll_email_nullable: public.yeoljeong_payroll_statements absent';
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE yeoljeong_payroll_statements ALTER COLUMN employee_email DROP NOT NULL';

    EXECUTE $sql$UPDATE yeoljeong_payroll_statements SET employee_email = NULL WHERE btrim(employee_email) = ''$sql$;
    EXECUTE $sql$UPDATE yeoljeong_payroll_statements SET employee_email = lower(btrim(employee_email))
 WHERE employee_email IS NOT NULL AND employee_email <> lower(btrim(employee_email))$sql$;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_yf_payroll_email_normalized'
           AND conrelid = 'public.yeoljeong_payroll_statements'::regclass
    ) THEN
        EXECUTE $sql$ALTER TABLE yeoljeong_payroll_statements
            ADD CONSTRAINT ck_yf_payroll_email_normalized
            CHECK (employee_email IS NULL OR (employee_email = lower(btrim(employee_email)) AND employee_email <> ''))
            NOT VALID$sql$;
    END IF;
    EXECUTE 'ALTER TABLE yeoljeong_payroll_statements VALIDATE CONSTRAINT ck_yf_payroll_email_normalized';

    EXECUTE $sql$CREATE INDEX IF NOT EXISTS idx_yf_payroll_email_pending
    ON yeoljeong_payroll_statements (business_id, employee_name)
    WHERE employee_email IS NULL AND deleted_at IS NULL$sql$;
END
$mig$;

COMMIT;
