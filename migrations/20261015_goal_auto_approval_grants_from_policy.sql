-- 20261015 goals.approval_policy → goal_auto_approval_grants 이관 (AA-1·AA-3·AA-4)
--
-- 정본: docs/prd/20261015_AADS_목표_우선순위_휴면정리_자동승인표시.md
--       (document_key=goal-priority-dormancy-autoapproval-display-prd) §5
-- 되돌림: migrations/rollback/20261015_goal_auto_approval_grants_from_policy.down.sql
-- 사전 점검: python3 scripts/goal_grant_migration_dry_run.py  (읽기 전용, 목표별 전후 비교)
--
-- 무엇을 하나
--   1) grant status 에 'paused' 를 더한다. 보류·초안·일시정지 목표의 grant 는 지우지도
--      회수하지도 않고 '정지' 로 둔다 — 재개 때 같은 설정으로 되살릴 근거가 남는다(AA-3).
--   2) 테넌트마다 다리 정책 버전 1행(mode=audit_only, project='LEGACY_APPROVAL_POLICY').
--      grant·판정 기록의 policy_version FK 를 채우기 위한 것이다. audit_only 라
--      aads_stale_policy_grants 가 기존 grant 를 stale 로 돌리지 않고, 프로젝트 표지가
--      실제 프로젝트와 달라 W-14 정책 조회(project=$2 OR project IS NULL)에 잡히지 않는다.
--   3) 정책 보유 목표마다 grant 1행. 만료 = 적용 시각 + 30일(AA-4).
--      action 은 'live_trading_gate' 하나 — live_trading_guard 가 이것만 본다.
--
-- 무엇을 하지 않나
--   - goals.approval_policy 는 건드리지 않는다(원본 보존, 이후 읽기 전용 호환 뷰).
--   - 목표 status 를 바꾸지 않는다.
--
-- 대응 규칙
--   auto_approve_critical=true → max_risk_tier A2 (주문·자금까지)
--   auto_approve_high=true     → A1 (고위험까지)
--   둘 다 false                → status revoked (revoke_reason=legacy_policy_disabled)
--   목표 status≠active 또는 paused_at → status paused (revoke_reason=goal_not_active:<status> | goal_paused)
--   used > max                 → used=max (CHECK used<=max)
--   요청·발급·승인 주체         → 00000000-0000-0000-0000-000000000000
--                                 (= approval_policy.set_by, 실제 값은 conditions.set_by)
--   assignment_id              → 주도 세션의 프로젝트 배정 → 없으면 그 프로젝트의 가장 오래된
--                                 배정 → 프로젝트에 배정이 하나도 없으면(FOOD·NTV2, 2026-10-15)
--                                 role_key='LegacyApprovalPolicyBridge' 배정을 주도 세션에 새로 만든다.
--
-- 재실행해도 같은 결과다. 이미 이관된 목표(되돌림으로 회수된 행 제외)는 건너뛴다.
BEGIN;

-- 1) paused 상태 ------------------------------------------------------------
ALTER TABLE goal_auto_approval_grants
    DROP CONSTRAINT IF EXISTS goal_auto_approval_grants_status_check;
ALTER TABLE goal_auto_approval_grants
    ADD CONSTRAINT goal_auto_approval_grants_status_check
    CHECK (status IN ('active','revoked','stale','expired','superseded','paused'));

-- 2) 다리 정책 버전 -----------------------------------------------------------
CREATE OR REPLACE FUNCTION aads_legacy_policy_bridge_hash(p_tenant_id UUID) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT 'sha256:' || encode(sha256(convert_to(
        'aads-legacy-approval-policy-bridge-v1:' || p_tenant_id::text, 'UTF8')), 'hex')
$$;

INSERT INTO goal_approval_policy_versions
    (tenant_id, project, policy, policy_hash, created_by, mode, simulation_result)
SELECT DISTINCT g.tenant_id, 'LEGACY_APPROVAL_POLICY',
       jsonb_build_object(
           'kind', 'legacy_approval_policy_bridge',
           'migration', '20261015_goal_auto_approval_grants_from_policy',
           'rules', jsonb_build_object(
               'auto_approve_critical', 'A2', 'auto_approve_high', 'A1',
               'default_expiry_days', 30, 'action', 'live_trading_gate')),
       aads_legacy_policy_bridge_hash(g.tenant_id),
       '00000000-0000-0000-0000-000000000000'::uuid, 'audit_only', '{}'::jsonb
  FROM goals g
 WHERE COALESCE(g.approval_policy, '{}'::jsonb) ?| ARRAY['auto_approve_high','auto_approve_critical']
ON CONFLICT (tenant_id, policy_hash) DO NOTHING;

-- 3) 이관 계획 (읽기 전용) ------------------------------------------------------
-- dry-run 스크립트가 @@PLAN_BEGIN~@@PLAN_END 사이 SELECT 를 그대로 읽어 돌린다
-- (p_goal_id → NULL). 계획 규칙은 여기 한 곳에만 둔다.
CREATE OR REPLACE FUNCTION aads_goal_policy_grant_plan(p_goal_id UUID DEFAULT NULL)
RETURNS TABLE (
    goal_id UUID, tenant_id UUID, project TEXT, goal_status TEXT, goal_paused BOOLEAN,
    principal_session_id UUID, assignment_id UUID, assignment_source TEXT,
    auto_high BOOLEAN, auto_critical BOOLEAN, max_risk_tier TEXT,
    max_executions INTEGER, used_executions INTEGER, legacy_used INTEGER,
    plan_status TEXT, plan_reason TEXT, legacy_policy JSONB
)
LANGUAGE sql STABLE AS $$
-- @@PLAN_BEGIN
SELECT g.id AS goal_id, g.tenant_id AS tenant_id, g.project AS project,
       g.status AS goal_status, g.paused_at IS NOT NULL AS goal_paused,
       g.owner_session_id AS principal_session_id,
       COALESCE(own.id, proj.id) AS assignment_id,
       CASE WHEN own.id IS NOT NULL THEN 'owner_assignment'
            WHEN proj.id IS NOT NULL THEN 'project_assignment'
            ELSE 'bridge_assignment' END AS assignment_source,
       f.high AS auto_high, f.critical AS auto_critical,
       CASE WHEN f.critical THEN 'A2' ELSE 'A1' END AS max_risk_tier,
       f.max_exec AS max_executions,
       LEAST(f.used, f.max_exec) AS used_executions,
       f.used AS legacy_used,
       CASE WHEN NOT (f.high OR f.critical) THEN 'revoked'
            WHEN g.status <> 'active' OR g.paused_at IS NOT NULL THEN 'paused'
            ELSE 'active' END AS plan_status,
       CASE WHEN NOT (f.high OR f.critical) THEN 'legacy_policy_disabled'
            WHEN g.status <> 'active' THEN 'goal_not_active:' || g.status
            WHEN g.paused_at IS NOT NULL THEN 'goal_paused'
            ELSE NULL END AS plan_reason,
       COALESCE(g.approval_policy, '{}'::jsonb) AS legacy_policy
  FROM goals g
  CROSS JOIN LATERAL (
      SELECT lower(COALESCE(g.approval_policy->>'auto_approve_high', '')) IN ('true','t','1','yes','on') AS high,
             lower(COALESCE(g.approval_policy->>'auto_approve_critical', '')) IN ('true','t','1','yes','on') AS critical,
             CASE WHEN COALESCE(g.approval_policy->>'max_executions', '') ~ '^[0-9]+$'
                  THEN (g.approval_policy->>'max_executions')::int ELSE 0 END AS max_exec,
             CASE WHEN COALESCE(g.approval_policy->>'used', '') ~ '^[0-9]+$'
                  THEN (g.approval_policy->>'used')::int ELSE 0 END AS used
  ) f
  LEFT JOIN LATERAL (
      SELECT a.id FROM project_role_assignments a
       WHERE a.tenant_id = g.tenant_id AND a.project = g.project AND a.active
         AND a.session_id = g.owner_session_id
       LIMIT 1
  ) own ON TRUE
  LEFT JOIN LATERAL (
      SELECT a.id FROM project_role_assignments a
       WHERE a.tenant_id = g.tenant_id AND a.project = g.project AND a.active
       ORDER BY a.assigned_at, a.id
       LIMIT 1
  ) proj ON TRUE
 WHERE g.status IN ('draft','active','blocked')
   AND COALESCE(g.approval_policy, '{}'::jsonb) ?| ARRAY['auto_approve_high','auto_approve_critical']
   AND g.owner_session_id IS NOT NULL
   AND (p_goal_id IS NULL OR g.id = p_goal_id)
 ORDER BY g.status, g.id
-- @@PLAN_END
$$;

-- 4) 목표 하나 이관 -----------------------------------------------------------
-- p_only_missing=true  : 이미 grant 가 있으면 그것을 돌려준다(일괄 이관·재실행).
-- p_only_missing=false : 기존 live/paused grant 를 superseded 로 돌리고 새로 낸다
--                        (POST /goals/{id}/approval-policy 저장 시).
CREATE OR REPLACE FUNCTION aads_migrate_goal_approval_policy(
    p_goal_id UUID, p_source TEXT DEFAULT 'migration_20261015', p_only_missing BOOLEAN DEFAULT TRUE
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    p RECORD;
    v_policy UUID;
    v_assignment UUID;
    v_existing UUID;
    v_new UUID;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_system CONSTANT UUID := '00000000-0000-0000-0000-000000000000';
BEGIN
    SELECT * INTO p FROM aads_goal_policy_grant_plan(p_goal_id) LIMIT 1;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT gr.id INTO v_existing
      FROM goal_auto_approval_grants gr
     WHERE gr.goal_id = p.goal_id AND gr.tenant_id = p.tenant_id
       AND 'live_trading_gate' = ANY(gr.actions)
       AND gr.revoke_reason IS DISTINCT FROM 'migration_rollback'
     ORDER BY gr.issued_at DESC
     LIMIT 1;
    IF p_only_missing AND v_existing IS NOT NULL THEN
        RETURN v_existing;
    END IF;

    SELECT v.id INTO v_policy FROM goal_approval_policy_versions v
     WHERE v.tenant_id = p.tenant_id AND v.policy_hash = aads_legacy_policy_bridge_hash(p.tenant_id);
    IF v_policy IS NULL THEN
        INSERT INTO goal_approval_policy_versions
            (tenant_id, project, policy, policy_hash, created_by, mode, simulation_result)
        VALUES (p.tenant_id, 'LEGACY_APPROVAL_POLICY',
                jsonb_build_object('kind', 'legacy_approval_policy_bridge',
                                   'migration', '20261015_goal_auto_approval_grants_from_policy'),
                aads_legacy_policy_bridge_hash(p.tenant_id), v_system, 'audit_only', '{}'::jsonb)
        RETURNING id INTO v_policy;
    END IF;

    v_assignment := p.assignment_id;
    IF v_assignment IS NULL THEN
        SELECT a.id INTO v_assignment FROM project_role_assignments a
         WHERE a.tenant_id = p.tenant_id AND a.project = p.project AND a.active
           AND a.role_key = 'LegacyApprovalPolicyBridge'
         LIMIT 1;
        IF v_assignment IS NULL THEN
            INSERT INTO project_role_assignments (tenant_id, project, role_key, session_id, active)
            VALUES (p.tenant_id, p.project, 'LegacyApprovalPolicyBridge', p.principal_session_id, TRUE)
            RETURNING id INTO v_assignment;
        END IF;
    END IF;

    UPDATE goal_auto_approval_grants
       SET status = 'superseded', revoked_at = v_now,
           revoke_reason = 'superseded_by_' || p_source
     WHERE goal_id = p.goal_id AND tenant_id = p.tenant_id
       AND 'live_trading_gate' = ANY(actions)
       AND status IN ('active', 'paused');

    INSERT INTO goal_auto_approval_grants
        (tenant_id, project, principal_session_id, assignment_id, goal_id,
         actions, tool_groups, max_risk_tier, environments, conditions,
         max_executions, used_executions, valid_from, expires_at,
         policy_version, grant_version, scope_hash, revocation_strategy, status,
         requested_by, issued_by, approved_by, revoked_at, revoke_reason, supersedes_grant_id)
    VALUES
        (p.tenant_id, p.project, p.principal_session_id, v_assignment, p.goal_id,
         ARRAY['live_trading_gate'], '{}', p.max_risk_tier, '{}',
         jsonb_build_object(
             'source', p_source,
             'set_by', p.legacy_policy->>'set_by',
             'set_at', p.legacy_policy->>'set_at',
             'auto_approve_high', p.auto_high,
             'auto_approve_critical', p.auto_critical,
             'legacy_used', p.legacy_used,
             'assignment_source', p.assignment_source,
             'legacy_policy', p.legacy_policy,
             'migrated_at', v_now),
         p.max_executions, p.used_executions, v_now, v_now + interval '30 days',
         v_policy, 1,
         'sha256:' || encode(sha256(convert_to(concat_ws(':',
             p.goal_id::text, p.max_risk_tier, p.max_executions::text,
             'live_trading_gate', v_now::text), 'UTF8')), 'hex'),
         'cancel_now', p.plan_status,
         v_system, v_system, v_system,
         CASE WHEN p.plan_status <> 'active' THEN v_now END,
         p.plan_reason, v_existing)
    RETURNING id INTO v_new;
    RETURN v_new;
END $$;

-- 5) 일괄 이관 ----------------------------------------------------------------
DO $$
DECLARE
    v_total INTEGER;
    v_active INTEGER;
    v_paused INTEGER;
    v_revoked INTEGER;
BEGIN
    PERFORM aads_migrate_goal_approval_policy(p.goal_id, 'migration_20261015', TRUE)
       FROM aads_goal_policy_grant_plan(NULL) p;
    SELECT count(*), count(*) FILTER (WHERE status = 'active'),
           count(*) FILTER (WHERE status = 'paused'), count(*) FILTER (WHERE status = 'revoked')
      INTO v_total, v_active, v_paused, v_revoked
      FROM goal_auto_approval_grants
     WHERE conditions->>'source' = 'migration_20261015'
       AND revoke_reason IS DISTINCT FROM 'migration_rollback';
    RAISE NOTICE 'goal grant migration: total=% active=% paused=% revoked=%',
        v_total, v_active, v_paused, v_revoked;
END $$;

COMMIT;
