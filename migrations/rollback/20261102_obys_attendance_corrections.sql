-- 비파괴 롤백: 라우터를 이전 릴리스로 되돌리고 요청·감사 이력은 보존한다.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'obys' THEN
        RAISE EXCEPTION 'OBYS business database required';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='acct_business_runtime_r5') THEN
        REVOKE INSERT,UPDATE ON public.obys_attendance_corrections FROM acct_business_runtime_r5;
    END IF;
END $$;
DROP TRIGGER IF EXISTS obys_correction_payroll_fence ON public.yeoljeong_payroll_statements;
DROP FUNCTION IF EXISTS public.obys_correction_payroll_fence();
COMMIT;
