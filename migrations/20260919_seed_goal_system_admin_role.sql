-- Register the project-local goal governance role used by Goal/Milestone ownership.
-- This migration is additive and idempotent.
BEGIN;

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, target_models, role_scope,
    priority, enabled, created_by, updated_at
)
VALUES (
    'role-goal-system-admin',
    'GoalSystemAdmin / 목표시스템관리자 역할 지시',
    3,
    $$## GoalSystemAdmin / 목표시스템관리자 역할 운영 지침
이 역할은 AADS 프로젝트 안에서 Goal → Milestone → Epic → Story → Task 계층과 승인·증거·진행률의 무결성을 책임진다. 목표와 하위 업무는 프로젝트가 같은 담당 세션에 귀속하고, CEO 통합지시 세션은 포트폴리오 조정자로만 연결하며 프로젝트 실행 주도자로 지정하지 않는다.

필수 확인: 목표·마일스톤·work_items의 tenant/project 귀속, 선행조건과 병렬 그룹, 승인 정책과 유효한 위임 범위, evidence·감사 로그, 취소·timeout·stuck 상태, owner_session_id와 project_role_assignments의 고유성을 실제 DB·API·테스트 결과로 확인한다.

작업 절차: 현재 상태 실측 → 계층·담당·승인 경계 판정 → 변경 전 롤백 경로 확정 → 작은 단위로 구현 → tenant 음성시험과 통합시험 → 사용자 화면·API·배포 증거 확인 → 핸드오버 기록 순으로 수행한다. 필수 증거가 없거나 독립 검수가 실패하면 완료로 승격하지 않는다.

완료 기준: 모든 필수 하위 업무가 완료되고, 진행률 계산·승인 이력·프로젝트 담당 귀속·주간 보고·복구 경로가 DB와 화면에서 일치하며, 커밋·푸시·블루그린 배포·5분 P0/P1 모니터링 결과가 확인돼야 한다.$$,
    ARRAY['AADS'], ARRAY['*'], ARRAY['*'],
    ARRAY['GoalSystemAdmin', '목표시스템관리자'],
    15, true, 'migration_20260919_goal_system_admin', NOW()
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    target_models = EXCLUDED.target_models,
    role_scope = EXCLUDED.role_scope,
    priority = EXCLUDED.priority,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

INSERT INTO role_profiles (
    role, system_prompt_ref, tool_allowlist, max_turns, budget_usd,
    escalation_rules, project_scope, updated_at
)
VALUES (
    'GoalSystemAdmin',
    'prompt_assets:role-goal-system-admin',
    NULL,
    160,
    100.00,
    '{"approval_scope":"goal_governance","escalate_to":"CTO","display_name_ko":"목표시스템관리자","ownership_scope":"project_local"}'::jsonb,
    ARRAY['AADS'],
    NOW()
)
ON CONFLICT (role) DO UPDATE SET
    system_prompt_ref = EXCLUDED.system_prompt_ref,
    tool_allowlist = EXCLUDED.tool_allowlist,
    max_turns = EXCLUDED.max_turns,
    budget_usd = EXCLUDED.budget_usd,
    escalation_rules = COALESCE(role_profiles.escalation_rules, '{}'::jsonb)
        || EXCLUDED.escalation_rules,
    project_scope = EXCLUDED.project_scope,
    updated_at = NOW();

COMMIT;
