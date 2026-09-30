-- 롤백: migrations/20260930_obys_employment_audit_lookup.sql
-- 인덱스만 걷는다. 감사로그 행은 건드리지 않는다.
DROP INDEX IF EXISTS idx_yeoljeong_audit_logs_resource;
