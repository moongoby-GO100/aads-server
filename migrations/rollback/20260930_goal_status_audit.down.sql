-- Rollback: AADS-GOAL-STATUS-WHITELIST-AUDIT-20260930
-- 수동 적용 전용(apply_release_migrations.sh 는 rollback/ 를 건너뛴다).
-- 주의: goals.status 변경 감사기록이 전부 사라진다. 코드(update_goal)는 테이블이 없어도
-- warning 만 남기고 계속 동작하므로 코드 롤백 없이 적용해도 안전하다.

DROP INDEX IF EXISTS idx_goal_status_audit_goal_changed;
DROP TABLE IF EXISTS goal_status_audit;
