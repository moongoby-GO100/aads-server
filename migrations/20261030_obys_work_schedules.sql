-- 오비서 근무 일정·근무표: 업무 DB 전용, 반복 적용 가능.
BEGIN;
CREATE TABLE IF NOT EXISTS yeoljeong_work_schedules (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id),
    employee_email TEXT NOT NULL CHECK (employee_email = lower(trim(employee_email)) AND employee_email <> ''),
    employee_email_masked TEXT NOT NULL DEFAULT '',
    employee_name TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    work_date DATE NOT NULL,
    start_time TIME,
    end_time TIME,
    break_minutes INTEGER NOT NULL DEFAULT 0 CHECK (break_minutes >= 0 AND break_minutes < 1440),
    kind TEXT NOT NULL DEFAULT 'work' CHECK (kind IN ('work','off','leave')),
    memo TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    CHECK (kind <> 'work' OR (start_time IS NOT NULL AND end_time IS NOT NULL AND start_time <> end_time
      AND break_minutes < ((extract(epoch FROM (end_time-start_time))::integer + 86400) % 86400)/60))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_obys_work_schedules_active
    ON yeoljeong_work_schedules (business_id, employee_email, work_date) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_obys_work_schedules_date
    ON yeoljeong_work_schedules (business_id, work_date);

-- 운영 앱은 acct_business_runtime_r5 로 접속한다. 권한이 없으면 근무표 API 가 permission denied 로 실패한다.
-- 삭제는 deleted_at 을 채우는 UPDATE 이므로 SELECT·INSERT·UPDATE 만 준다.
DO $grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acct_business_runtime_r5') THEN
        GRANT SELECT, INSERT, UPDATE ON public.yeoljeong_work_schedules TO acct_business_runtime_r5;
    ELSE
        RAISE NOTICE 'role acct_business_runtime_r5 not found; grants skipped';
    END IF;
END
$grant$;

COMMIT;
