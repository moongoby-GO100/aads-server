-- 오비서 근무표 되돌리기: 먼저 이전 애플리케이션으로 전환하고 데이터를 백업한다.
-- 행을 보존한 채 테이블 이름을 바꾼다. 재적용 전에 보관 테이블을 복원한다.
BEGIN;
ALTER TABLE IF EXISTS yeoljeong_work_schedules RENAME TO yeoljeong_work_schedules_20261030_archive;
COMMIT;
