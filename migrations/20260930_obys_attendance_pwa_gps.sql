-- AADS-OBYS-PWA-GPS-ATTENDANCE-20260930
-- 오비서 PWA 출퇴근 1차 — 지점 좌표·반경, 근태 행의 출퇴근 시각·위치 1점·판정·동의 시각.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_attendance_pwa_gps.sql
--   절차: docs/operations/OBYS_ATTENDANCE_PWA_GPS.md
-- 스키마 추가만 한다. ADD COLUMN 은 전부 NULL 허용·기본값 없음이라 기존 근태·지점 행을
--   다시 쓰지 않고, 기존 근태의 status 를 재판정하지 않는다(신규 PWA 기록부터 적용).
-- source 에 CHECK 가 있으면 기존 조건에 `OR source = 'pwa'` 만 더한다(허용 범위 확장).
--   원래 조건은 새 제약의 COMMENT 에 남겨 롤백이 그대로 되돌린다.
-- 멱등하다(IF NOT EXISTS, 이미 확장된 제약은 건너뜀).
-- 롤백: migrations/rollback/20260930_obys_attendance_pwa_gps.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_attendance_records') IS NULL
       OR to_regclass('public.yeoljeong_branches') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_attendance_records / yeoljeong_branches 없음)';
    END IF;
END
$$;

-- 1) 지점 좌표·반경. 반경이 NULL 이면 코드 기본값(GEOFENCE_DEFAULT_RADIUS_M)을 쓴다.
ALTER TABLE yeoljeong_branches ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
ALTER TABLE yeoljeong_branches ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
ALTER TABLE yeoljeong_branches ADD COLUMN IF NOT EXISTS geofence_radius_m INTEGER;

-- 2) 근태 행: 버튼을 누른 순간의 시각·위치 1점·판정.
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_in_at TIMESTAMPTZ;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_out_at TIMESTAMPTZ;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_in_lat DOUBLE PRECISION;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_in_lng DOUBLE PRECISION;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_in_accuracy_m INTEGER;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_out_lat DOUBLE PRECISION;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_out_lng DOUBLE PRECISION;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_out_accuracy_m INTEGER;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_in_distance_m INTEGER;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS check_out_distance_m INTEGER;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS geofence_result TEXT;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS device_info TEXT;
ALTER TABLE yeoljeong_attendance_records ADD COLUMN IF NOT EXISTS location_consent_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.yeoljeong_attendance_records'::regclass
           AND conname = 'yeoljeong_attendance_records_geofence_result_check'
    ) THEN
        ALTER TABLE yeoljeong_attendance_records
            ADD CONSTRAINT yeoljeong_attendance_records_geofence_result_check
            CHECK (geofence_result IS NULL OR geofence_result IN ('inside', 'outside', 'unknown'));
    END IF;
END
$$;

-- 3) source = 'pwa' 허용. CHECK 가 없으면 할 일이 없다(코드 상수 ATTENDANCE_SOURCES 가 정본).
DO $$
DECLARE
    con RECORD;
BEGIN
    FOR con IN
        SELECT DISTINCT c.oid, c.conname, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = 'public.yeoljeong_attendance_records'::regclass
           AND c.contype = 'c'
           AND a.attname = 'source'
           AND c.conname <> 'yeoljeong_attendance_records_source_pwa_check'
    LOOP
        IF position('''pwa''' IN con.def) > 0 THEN
            CONTINUE;  -- 이미 pwa 를 허용한다
        END IF;
        -- def 는 'CHECK (...)' 형태다. 조건부만 떼어 OR 로 넓힌다.
        EXECUTE format(
            'ALTER TABLE yeoljeong_attendance_records ADD CONSTRAINT yeoljeong_attendance_records_source_pwa_check CHECK ((%s) OR source = %L)',
            substring(con.def FROM 7), 'pwa'
        );
        EXECUTE format(
            'COMMENT ON CONSTRAINT yeoljeong_attendance_records_source_pwa_check ON yeoljeong_attendance_records IS %L',
            json_build_object('original_name', con.conname, 'original_def', con.def)::text
        );
        EXECUTE format('ALTER TABLE yeoljeong_attendance_records DROP CONSTRAINT %I', con.conname);
        RAISE NOTICE 'source CHECK % 를 pwa 허용으로 확장했다: %', con.conname, con.def;
        EXIT;  -- source 제약은 하나만 확장한다
    END LOOP;
END
$$;

-- 4) 열린 출근(퇴근 전) 조회용.
CREATE INDEX IF NOT EXISTS idx_yeoljeong_attendance_pwa_open
    ON yeoljeong_attendance_records (business_id, employee_email, work_date)
    WHERE source = 'pwa' AND check_out_at IS NULL AND deleted_at IS NULL;

-- 5) 런타임 롤(카페24 acct-pg)이 새 컬럼을 읽고 쓰게 한다. 롤이 없는 DB(진아서버 등)는 건너뛴다.
--   이미 테이블 단위 권한이 있으면 변화 없다(GRANT 는 멱등).
DO $grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acct_business_runtime_r5') THEN
        GRANT SELECT, INSERT, UPDATE ON public.yeoljeong_attendance_records TO acct_business_runtime_r5;
        GRANT SELECT, UPDATE ON public.yeoljeong_branches TO acct_business_runtime_r5;
    ELSE
        RAISE NOTICE 'role acct_business_runtime_r5 not found; grants skipped';
    END IF;
END
$grant$;

COMMIT;
