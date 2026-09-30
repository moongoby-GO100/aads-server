-- Rollback: AADS-OBYS-PWA-GPS-ATTENDANCE-20260930
-- 오비서 업무 DB 전용, 수동 적용. AADS DB 에 돌리지 않는다.
--   psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/rollback/20260930_obys_attendance_pwa_gps.down.sql
--
-- 주의: PWA 로 기록된 근태(source='pwa')가 하나라도 있으면 멈춘다. 그 행의 출퇴근
-- 위치·판정·동의 시각은 이 컬럼에만 있어서, 컬럼을 지우면 근거가 사라진다.
-- 먼저 코드를 되돌려 신규 PWA 기록을 막고, 남길 행을 내보낸 뒤(운영 문서 §7 쿼리)
-- 관리자 판단으로 처리한 다음 실행한다. 기존 수기·CSV 근태는 영향 없다.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_attendance_records') IS NULL
       OR to_regclass('public.yeoljeong_branches') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다';
    END IF;
    IF EXISTS (SELECT 1 FROM yeoljeong_attendance_records WHERE source = 'pwa') THEN
        RAISE EXCEPTION 'source=pwa 근태가 남아 있어 롤백하지 않는다 — docs/operations/OBYS_ATTENDANCE_PWA_GPS.md §8';
    END IF;
END
$$;

-- source CHECK 를 원래 조건으로 되돌린다(확장하지 않았으면 할 일 없음).
DO $$
DECLARE
    saved JSONB;
BEGIN
    SELECT obj_description(c.oid, 'pg_constraint')::jsonb INTO saved
      FROM pg_constraint c
     WHERE c.conrelid = 'public.yeoljeong_attendance_records'::regclass
       AND c.conname = 'yeoljeong_attendance_records_source_pwa_check';
    IF saved IS NULL THEN
        RETURN;
    END IF;
    EXECUTE format('ALTER TABLE yeoljeong_attendance_records ADD CONSTRAINT %I %s',
                   saved->>'original_name', saved->>'original_def');
    ALTER TABLE yeoljeong_attendance_records DROP CONSTRAINT yeoljeong_attendance_records_source_pwa_check;
END
$$;

DROP INDEX IF EXISTS idx_yeoljeong_attendance_pwa_open;
ALTER TABLE yeoljeong_attendance_records DROP CONSTRAINT IF EXISTS yeoljeong_attendance_records_geofence_result_check;

ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_in_at;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_out_at;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_in_lat;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_in_lng;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_in_accuracy_m;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_out_lat;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_out_lng;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_out_accuracy_m;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_in_distance_m;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS check_out_distance_m;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS geofence_result;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS device_info;
ALTER TABLE yeoljeong_attendance_records DROP COLUMN IF EXISTS location_consent_at;

-- 지점 좌표는 매장 위치(개인정보 아님)지만 이 기능 전용이므로 함께 걷는다.
ALTER TABLE yeoljeong_branches DROP COLUMN IF EXISTS latitude;
ALTER TABLE yeoljeong_branches DROP COLUMN IF EXISTS longitude;
ALTER TABLE yeoljeong_branches DROP COLUMN IF EXISTS geofence_radius_m;

COMMIT;
