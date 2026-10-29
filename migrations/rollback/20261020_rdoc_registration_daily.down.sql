-- 20261020_rdoc_registration_daily.sql 롤백(승인 뒤 migrations/rollback/ 으로 함께 옮긴다). 수집 행(실측 증거)이 사라지므로
-- 먼저 `\copy (SELECT * FROM rdoc_registration_daily) TO backup.csv CSV HEADER` 로 내려받는다.
BEGIN;
DROP INDEX IF EXISTS rdoc_registration_daily_date_idx;
DROP TABLE IF EXISTS rdoc_registration_daily;
COMMIT;
