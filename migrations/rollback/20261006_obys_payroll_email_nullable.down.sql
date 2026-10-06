-- employee_email NULL 허용을 되돌린다. 삭제되지 않은(deleted_at IS NULL) 행 중 이메일이 NULL 인 것이 남아 있으면 중단한다 —
-- 먼저 그 행을 deleted_at 처리하거나 이메일을 채운 뒤 다시 실행한다. 활성 행을 ''로 바꿔 통과시키지 않는다.
-- 이미 소프트삭제된 NULL 행은 SET NOT NULL 을 막으므로, 이 migration 이전 표현('')으로만 되돌린다.
BEGIN;

DO $$
DECLARE
    pending_rows BIGINT;
BEGIN
    SELECT count(*) INTO pending_rows
      FROM yeoljeong_payroll_statements WHERE employee_email IS NULL AND deleted_at IS NULL;
    IF pending_rows > 0 THEN
        RAISE EXCEPTION 'yeoljeong_payroll_statements 에 employee_email NULL 활성 행이 % 건 남아 있습니다. 먼저 해당 행을 deleted_at 처리하거나 이메일을 채우십시오.', pending_rows;
    END IF;
END $$;

UPDATE yeoljeong_payroll_statements SET employee_email = '' WHERE employee_email IS NULL AND deleted_at IS NOT NULL;
ALTER TABLE yeoljeong_payroll_statements DROP CONSTRAINT IF EXISTS ck_yf_payroll_email_normalized;
DROP INDEX IF EXISTS idx_yf_payroll_email_pending;
ALTER TABLE yeoljeong_payroll_statements ALTER COLUMN employee_email SET NOT NULL;

COMMIT;
