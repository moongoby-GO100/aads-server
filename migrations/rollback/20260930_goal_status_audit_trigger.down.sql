-- Rollback: AADS-GOAL-STATUS-AUDIT-DB-TRIGGER-20260930
-- 수동 적용 전용(apply_release_migrations.sh 는 rollback/ 를 건너뛴다).
-- 트리거 2개와 함수 2개만 내린다. goal_status_audit 테이블과 이미 쌓인 감사 행은 그대로 둔다.
-- 내린 뒤에는 다시 goal_manager.update_goal 경로만 기록되고 자동경로는 무감사가 된다.

DROP TRIGGER IF EXISTS trg_goal_status_audit_capture ON goals;
DROP TRIGGER IF EXISTS trg_goal_status_audit_merge ON goal_status_audit;
DROP FUNCTION IF EXISTS goal_status_audit_capture();
DROP FUNCTION IF EXISTS goal_status_audit_merge();
