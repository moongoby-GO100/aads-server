-- AADS-OBYS-MULTISTORE-EMPLOYMENT-KEY-20261001
-- 오비서 멀티매장 겸직 — 출퇴근 유니크 키에 business_id 를 넣는다.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20261001_obys_attendance_business_scoped_unique.sql
--   contabo116 사본: docker exec -i aads-postgres psql -U aads -d obys -v ON_ERROR_STOP=1 < 이 파일
--   진아서버 운영 반영은 별도 승인 릴리스에서 한다.
--
-- 배경: 겸직 직원의 매장(=별개 사업자)마다 주간집계·근태가 따로 있어야 한다.
--   yeoljeong_attendance_weekly  UNIQUE(employee_email, week_start)
--     -> UNIQUE(employee_email, business_id, week_start)
--        (주 15시간·주휴수당 판정은 사업장별이라 직원당 1행이면 매장별 집계가 서로 덮어쓴다)
--   yeoljeong_attendance_records UNIQUE(employee_email, work_date, start_at)
--     -> UNIQUE(employee_email, business_id, work_date, start_at)
--        (같은 날 같은 시작시각이면 두 매장 출근을 기록할 수 없다)
-- 새 키는 옛 키보다 느슨하다(컬럼 추가)라 기존 행이 새 키와 충돌할 수 없지만, 사전 점검을
--   넣어 예상 밖 상태(NULL business_id·중복)면 실패한다.
-- 옛 제약은 이름이 아니라 컬럼 구성으로 찾는다(DB 별로 이름이 다를 수 있다). 멱등하다.
-- 코드: app/api/obys_workspaces.py 의 ON CONFLICT 대상이 새 키와 같아야 한다. 이 마이그레이션이
--   먼저 적용돼야 새 코드의 INSERT 가 동작한다(ON CONFLICT 추론 대상 유니크가 있어야 한다).
-- 롤백: migrations/rollback/20261001_obys_attendance_business_scoped_unique.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_attendance_records') IS NULL
       OR to_regclass('public.yeoljeong_attendance_weekly') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_attendance_records / yeoljeong_attendance_weekly 없음)';
    END IF;
END
$$;

-- 사전 점검: 새 키 기준 중복 그룹이 있으면 중단한다.
DO $$
DECLARE
    dup_count BIGINT;
BEGIN
    SELECT count(*) INTO dup_count FROM (
        SELECT employee_email, business_id, week_start
          FROM yeoljeong_attendance_weekly
         GROUP BY employee_email, business_id, week_start
        HAVING count(*) > 1
    ) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'yeoljeong_attendance_weekly 에 (employee_email, business_id, week_start) 중복 그룹이 % 건 있다 — 정리 후 다시 실행', dup_count;
    END IF;

    SELECT count(*) INTO dup_count FROM (
        SELECT employee_email, business_id, work_date, start_at
          FROM yeoljeong_attendance_records
         GROUP BY employee_email, business_id, work_date, start_at
        HAVING count(*) > 1
    ) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'yeoljeong_attendance_records 에 (employee_email, business_id, work_date, start_at) 중복 그룹이 % 건 있다 — 정리 후 다시 실행', dup_count;
    END IF;

    SELECT count(*) INTO dup_count FROM yeoljeong_attendance_weekly WHERE business_id IS NULL OR business_id = '';
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'yeoljeong_attendance_weekly 에 business_id 가 빈 행이 % 건 있다 — 사업자 귀속 후 다시 실행', dup_count;
    END IF;

    SELECT count(*) INTO dup_count FROM yeoljeong_attendance_records WHERE business_id IS NULL OR business_id = '';
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'yeoljeong_attendance_records 에 business_id 가 빈 행이 % 건 있다 — 사업자 귀속 후 다시 실행', dup_count;
    END IF;
END
$$;

-- 1) yeoljeong_attendance_weekly
DO $$
DECLARE
    con RECORD;
BEGIN
    FOR con IN
        SELECT c.conname
          FROM pg_constraint c
         WHERE c.conrelid = 'public.yeoljeong_attendance_weekly'::regclass
           AND c.contype = 'u'
           AND (SELECT array_agg(a.attname::text ORDER BY a.attname)
                  FROM pg_attribute a
                 WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey))
               = ARRAY['employee_email', 'week_start']
    LOOP
        EXECUTE format('ALTER TABLE yeoljeong_attendance_weekly DROP CONSTRAINT %I', con.conname);
        RAISE NOTICE 'yeoljeong_attendance_weekly 옛 유니크 % 를 제거했다', con.conname;
    END LOOP;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.yeoljeong_attendance_weekly'::regclass
           AND conname = 'yeoljeong_attendance_weekly_email_biz_week_key'
    ) THEN
        ALTER TABLE yeoljeong_attendance_weekly
            ADD CONSTRAINT yeoljeong_attendance_weekly_email_biz_week_key
            UNIQUE (employee_email, business_id, week_start);
    END IF;
END
$$;

-- 2) yeoljeong_attendance_records
DO $$
DECLARE
    con RECORD;
BEGIN
    FOR con IN
        SELECT c.conname
          FROM pg_constraint c
         WHERE c.conrelid = 'public.yeoljeong_attendance_records'::regclass
           AND c.contype = 'u'
           AND (SELECT array_agg(a.attname::text ORDER BY a.attname)
                  FROM pg_attribute a
                 WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey))
               = ARRAY['employee_email', 'start_at', 'work_date']
    LOOP
        EXECUTE format('ALTER TABLE yeoljeong_attendance_records DROP CONSTRAINT %I', con.conname);
        RAISE NOTICE 'yeoljeong_attendance_records 옛 유니크 % 를 제거했다', con.conname;
    END LOOP;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.yeoljeong_attendance_records'::regclass
           AND conname = 'yeoljeong_attendance_records_email_biz_date_start_key'
    ) THEN
        ALTER TABLE yeoljeong_attendance_records
            ADD CONSTRAINT yeoljeong_attendance_records_email_biz_date_start_key
            UNIQUE (employee_email, business_id, work_date, start_at);
    END IF;
END
$$;

COMMIT;
