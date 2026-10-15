-- 20261015_goal_auto_approval_grants_from_policy 되돌림.
--
-- 행을 지우지 않는다. goal_policy_decisions·사용 기록이 grant 를 FK(RESTRICT)로
-- 가리키고, 판정 기록은 append-only 다. 대신 이관이 만든 grant 를 전부
-- revoke_reason='migration_rollback' 으로 회수한다 — live_trading_guard 는 이 행을
-- "grant 없음" 으로 보고 goals.approval_policy(이관 중 손대지 않은 원본)로 돌아간다.
--
-- 남는 것: 다리 정책 버전 행(goal_approval_policy_versions 는 수정·삭제 금지 트리거),
-- 비활성화된 LegacyApprovalPolicyBridge 배정 행, 회수된 grant 행. 모두 감사 근거다.
BEGIN;

UPDATE goal_auto_approval_grants
   SET status = CASE WHEN status IN ('active', 'paused') THEN 'revoked' ELSE status END,
       revoked_at = COALESCE(revoked_at, clock_timestamp()),
       revoke_reason = 'migration_rollback'
 WHERE 'live_trading_gate' = ANY(actions)
   AND conditions ? 'legacy_policy'
   AND revoke_reason IS DISTINCT FROM 'migration_rollback';

UPDATE project_role_assignments
   SET active = FALSE, ended_at = clock_timestamp(), end_reason = 'migration_rollback'
 WHERE role_key = 'LegacyApprovalPolicyBridge' AND active;

DROP FUNCTION IF EXISTS aads_migrate_goal_approval_policy(UUID, TEXT, BOOLEAN);
DROP FUNCTION IF EXISTS aads_goal_policy_grant_plan(UUID);
-- aads_legacy_policy_bridge_hash 는 남긴다 — 재적용 시 같은 다리 정책 행을 찾는 열쇠다.

-- paused 행이 남아 있으면 아래 CHECK 가 실패한다(이관 밖에서 만든 paused 가 있다는 뜻 —
-- 멈추고 확인한다).
ALTER TABLE goal_auto_approval_grants
    DROP CONSTRAINT IF EXISTS goal_auto_approval_grants_status_check;
ALTER TABLE goal_auto_approval_grants
    ADD CONSTRAINT goal_auto_approval_grants_status_check
    CHECK (status IN ('active','revoked','stale','expired','superseded'));

COMMIT;
