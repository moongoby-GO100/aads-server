-- 075: Fix CEO workspace L3 matching for PromptContextHarnessEngineer.
-- Created: 2026-04-29
--
-- Problem:
-- - CEO orchestration sessions can select PromptContextHarnessEngineer, but the
--   L3 prompt asset was scoped only to AADS.
-- - As a result, current CEO sessions received L1/L2/L4/L5, but no L3 role
--   prompt for PromptContextHarnessEngineer.
--
-- Design:
-- - Let the generic PromptContextHarnessEngineer role match CEO.
-- - Add a CEO-specific overlay instead of reusing the AADS overlay.
-- - Keep the existing role key unchanged for session/dropdown compatibility.

BEGIN;

UPDATE prompt_assets
SET workspace_scope = (
        SELECT ARRAY(
            SELECT DISTINCT value
            FROM unnest(COALESCE(workspace_scope, ARRAY[]::text[]) || ARRAY['CEO']::text[]) AS value
            WHERE value IS NOT NULL AND value <> ''
            ORDER BY value
        )
    ),
    intent_scope = (
        SELECT ARRAY(
            SELECT DISTINCT value
            FROM unnest(COALESCE(intent_scope, ARRAY[]::text[]) || ARRAY['*']::text[]) AS value
            WHERE value IS NOT NULL AND value <> ''
            ORDER BY value
        )
    ),
    updated_at = NOW()
WHERE slug = 'role-prompt-context-harness-engineer';

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, target_models, role_scope,
    priority, enabled, created_by, updated_at
)
VALUES (
    'project-role-ceo-prompt-context-harness',
    'CEO > PromptContextHarnessEngineer / 프롬프트·컨텍스트·하네스엔지니어 통합지시 오버레이',
    3,
    $$## CEO > PromptContextHarnessEngineer / 프롬프트·컨텍스트·하네스엔지니어 통합지시 오버레이
역할 정체성: CEO 통합지시에서 이 역할은 AADS 프롬프트 거버넌스와 6개 프로젝트 역할·지시·모델 라우팅 체계가 실제 세션에 적용되는지 검증한다.
전문 판단 기준: prompt_assets에 존재하는 것과 compiled_prompt_provenance에 실제 적용된 것을 분리해 판단한다. L1/L2/L3/L4/L5, role_key, workspace key, intent, target model, priority, enabled, role_profiles.project_scope를 함께 본다.
필수 확인: 현재 세션과 대상 세션의 chat_sessions.role_key, chat_workspaces.name/settings, prompt_assets scope, role_profiles, compiled_prompt_provenance.applied_assets, system_prompt_chars, compile_error를 조회한다.
작업 절차: 원인 진단은 DB 실측으로 시작하고, scope 누락이면 마이그레이션으로 보정하며, 적용 후 샘플 매칭 쿼리와 실제 다음 턴 provenance로 검증한다.
산출물 기준: 보고에는 적용/미적용 레이어, 누락 slug, 수정 SQL 파일, 전후 매칭 결과, 재시작 필요 여부, 남은 검증을 포함한다.
금지 행동: 프롬프트가 붙었다고 추정하지 말고 provenance 근거 없이 완료 선언하지 않는다. 프로젝트별 오버레이를 공통 역할에 섞어 책임 경계를 흐리지 않는다.$$,
    '{CEO}',
    '{prompt_engineering,context_engineering,harness,admin_ui,code_modify,cto_verify,status_check,task_query,*}',
    '{*}',
    '{PromptContextHarnessEngineer,PromptEngineer,ContextEngineer,HarnessEngineer,프롬프트엔지니어,컨텍스트엔지니어,하네스엔지니어}',
    20,
    true,
    'migration_075',
    NOW()
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    target_models = EXCLUDED.target_models,
    role_scope = EXCLUDED.role_scope,
    priority = EXCLUDED.priority,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

UPDATE role_profiles
SET project_scope = (
        SELECT ARRAY(
            SELECT DISTINCT value
            FROM unnest(COALESCE(project_scope, ARRAY[]::text[]) || ARRAY['CEO']::text[]) AS value
            WHERE value IS NOT NULL AND value <> ''
            ORDER BY value
        )
    ),
    escalation_rules = COALESCE(escalation_rules, '{}'::jsonb) || jsonb_build_object(
        'requires_provenance_check', true,
        'quality_rubric_version', 'prompt-context-harness-ceo-v1',
        'when_to_use', '프롬프트 레이어, 역할 적용, 시스템 프롬프트 누락, provenance 검증, 모델 라우팅, 하네스 품질 문제를 점검할 때 사용',
        'how_to_instruct', '대상 세션 ID, 기대 역할, 문제 증상, 확인할 레이어를 함께 주고 DB/provenance 기준으로 적용 여부와 원인을 보고하라고 지시'
    ),
    updated_at = NOW()
WHERE role = 'PromptContextHarnessEngineer';

COMMIT;
