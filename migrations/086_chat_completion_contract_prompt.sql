-- 086_chat_completion_contract_prompt.sql
-- Chat final-response completion contract.
-- The runtime hard guard lives in app/services/response_completion_contract.py.

BEGIN;

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, role_scope, target_models,
    priority, enabled, created_by, created_at, updated_at
) VALUES (
    'global-chat-completion-contract',
    '채팅 작업 완료 계약 (L1)',
    1,
    $CONTRACT$
## 작업 완료 계약
코드, 파일, DB, 배포, 운영 조치, 문서 변경을 수행한 응답은 마지막에 실제 완료 상태를 명시한다.

필수 항목:
1. 변경 파일 또는 대상 시스템.
2. 실행한 검증 명령/도구와 결과.
3. 커밋, 푸시, 문서기록, 배포 상태.
4. 완료하지 못한 항목과 그 사유.

금지:
- 커밋하지 않았는데 "커밋 완료"라고 말하지 않는다.
- 푸시하지 않았는데 "푸시 완료"라고 말하지 않는다.
- 문서기록을 하지 않았는데 "문서기록 완료"라고 말하지 않는다.
- 배포하지 않았거나 검증하지 않았는데 "배포 완료/정상"이라고 말하지 않는다.

세션 workspace ledger, git status, 배포/헬스 결과와 응답 내용이 다르면 실제 조회 결과를 우선하고, 미완료 상태를 명시한다.
$CONTRACT$,
    NULL,
    ARRAY['*'],
    NULL,
    NULL,
    6,
    TRUE,
    'migration_086',
    NOW(),
    NOW()
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    role_scope = EXCLUDED.role_scope,
    target_models = EXCLUDED.target_models,
    priority = EXCLUDED.priority,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

DO $$
DECLARE
    v_exists BOOL;
BEGIN
    SELECT EXISTS(
        SELECT 1
        FROM prompt_assets
        WHERE slug = 'global-chat-completion-contract'
          AND enabled = TRUE
          AND layer_id = 1
    ) INTO v_exists;

    IF NOT v_exists THEN
        RAISE EXCEPTION 'global-chat-completion-contract prompt asset was not applied';
    END IF;
END $$;

COMMIT;
