-- Rollback: AADS-OBYS-MULTISTORE-EMPLOYMENT-KEY-20261001
-- 오비서 업무 DB 전용, 수동 적용. AADS DB 에 돌리지 않는다.
--   psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/rollback/20261001_obys_attendance_business_scoped_unique.down.sql
--
-- 옛 키(business_id 없음)로 되돌린다. 겸직 기록이 이미 생겨 같은 (employee_email, week_start) /
-- (employee_email, work_date, start_at) 가 매장별로 2행 이상이면 옛 키를 만들 수 없으므로 멈춘다.
-- 그 행은 지우지 않는다 — 금전·법적 근거(주휴수당 판정)이므로 관리자 판단으로 정리한 뒤 다시 실행한다.
-- 롤백 후에는 app/api/obys_workspaces.py 의 ON CONFLICT 대상도 옛 키로 되돌려야 한다.

BEGIN;

DO $$
DECLARE
    dup_count BIGINT;
BEGIN
    IF to_regclass('public.yeoljeong_attendance_records') IS NULL
       OR to_regclass('public.yeoljeong_attendance_weekly') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다';
    END IF;

    SELECT count(*) INTO dup_count FROM (
        SELECT employee_email, week_start FROM yeoljeong_attendance_weekly
         GROUP BY employee_email, week_start HAVING count(*) > 1
    ) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION '겸직 주간집계가 매장별로 나뉘어 있어(% 그룹) 옛 키로 되돌릴 수 없다', dup_count;
    END IF;

    SELECT count(*) INTO dup_count FROM (
        SELECT employee_email, work_date, start_at FROM yeoljeong_attendance_records
         GROUP BY employee_email, work_date, start_at HAVING count(*) > 1
    ) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION '겸직 근태가 매장별로 나뉘어 있어(% 그룹) 옛 키로 되돌릴 수 없다', dup_count;
    END IF;
END
$$;

ALTER TABLE yeoljeong_attendance_weekly DROP CONSTRAINT IF EXISTS yeoljeong_attendance_weekly_email_biz_week_key;
ALTER TABLE yeoljeong_attendance_records DROP CONSTRAINT IF EXISTS yeoljeong_attendance_records_email_biz_date_start_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.yeoljeong_attendance_weekly'::regclass
           AND conname = 'yeoljeong_attendance_weekly_employee_email_week_start_key'
    ) THEN
        ALTER TABLE yeoljeong_attendance_weekly
            ADD CONSTRAINT yeoljeong_attendance_weekly_employee_email_week_start_key
            UNIQUE (employee_email, week_start);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.yeoljeong_attendance_records'::regclass
           AND conname = 'yeoljeong_attendance_records_employee_email_work_date_start_key'
    ) THEN
        ALTER TABLE yeoljeong_attendance_records
            ADD CONSTRAINT yeoljeong_attendance_records_employee_email_work_date_start_key
            UNIQUE (employee_email, work_date, start_at);
    END IF;
END
$$;

COMMIT;
