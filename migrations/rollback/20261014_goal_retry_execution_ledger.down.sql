-- Down migration for migrations/20261014_goal_retry_execution_ledger.sql (수동 적용, 허용목록 밖).
-- 코드는 테이블이 없으면 차감 단계가 fail closed 가 되므로, 롤백 전에 이전 릴리스(차감 호출 없는 러너)로 돌아가야 한다.
-- 장부 행은 실제 모델 실행 근거다. 버려도 되는지 확인한 뒤 실행한다.
BEGIN;
DROP FUNCTION IF EXISTS charge_goal_retry_execution(text, integer);
DROP TABLE IF EXISTS goal_retry_executions;
COMMIT;
