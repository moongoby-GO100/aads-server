-- Down migration for migrations/20261010_runner_host_policy.sql (수동 적용, 허용목록 밖).
-- 행이 없으면 러너는 env 동작으로 돌아가므로 코드는 그대로 두어도 된다. 감사 이력까지 버려도 되는지 확인 후 실행한다.
BEGIN;
DROP TRIGGER IF EXISTS trg_runner_host_policy_audit ON runner_host_policy;
DROP TRIGGER IF EXISTS trg_runner_host_policy_bump_revision ON runner_host_policy;
DROP FUNCTION IF EXISTS runner_host_policy_write_audit();
DROP FUNCTION IF EXISTS runner_host_policy_bump_revision();
DROP TABLE IF EXISTS runner_host_policy_audit;
DROP TABLE IF EXISTS runner_host_policy;
COMMIT;
