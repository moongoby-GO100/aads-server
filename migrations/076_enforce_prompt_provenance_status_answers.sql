-- 076: Enforce provenance-based answers for prompt/session status questions.
-- Created: 2026-04-29
--
-- Problem:
-- - Some sessions had valid compiled prompts in compiled_prompt_provenance, but
--   the assistant answered from workspace identity text and reported that the
--   system prompt was missing.
--
-- Design:
-- - Make L1 layer governance treat compiled_prompt_provenance as the source of
--   truth for prompt application status.
-- - Make L4 status_check responses use the same DB/provenance checklist before
--   answering system-prompt, role-prompt, or layer-application questions.

BEGIN;

UPDATE prompt_assets
SET content = content || $$ 

8. 시스템 프롬프트 적용 판정: 사용자가 "시스템 프롬프트가 들어왔나", "역할 프롬프트가 적용됐나", "L1/L2/L3/L4/L5가 붙었나"를 묻는 경우, 답변의 최종 근거는 `compiled_prompt_provenance`의 `system_prompt_chars`, `provenance.applied_assets`, `provenance.workspace`, `provenance.role`, `compile_error`이다. 워크스페이스 고정 정체성 문구, 모델의 자기소개, 이전 메시지 본문만으로 적용 여부를 판정하지 않는다.
9. 충돌 처리: 모델 응답 내용이 세션 metadata 또는 provenance와 다르면 provenance를 우선하고, "프롬프트 미적용"이 아니라 "적용된 프롬프트와 답변 내용의 불일치"로 분리해 보고한다. L3 누락은 role_key, role_scope, workspace_scope, intent_scope, enabled, priority를 조회해 원인을 좁힌다.$$,
    updated_at = NOW()
WHERE slug = 'global-layer-governance'
  AND layer_id = 1
  AND content NOT LIKE '%시스템 프롬프트 적용 판정:%';

UPDATE prompt_assets
SET content = content || $$

## 프롬프트 적용 상태 조회
- "시스템 프롬프트가 안 들어왔다", "역할이 적용됐나", "L3가 빠졌나" 같은 질문은 일반 상태조회가 아니라 prompt provenance 조회로 처리한다.
- 최소 확인값: `chat_sessions.id`, `chat_sessions.workspace_id`, `chat_sessions.role_key`, 최신 `compiled_prompt_provenance.system_prompt_chars`, `provenance.workspace`, `provenance.role`, `provenance.applied_assets`, `compile_error`.
- 판정 기준: `system_prompt_chars > 0`이고 `compile_error`가 없으며 `applied_assets`가 있으면 시스템 프롬프트는 적용된 것으로 본다. L3 적용 여부는 `applied_assets` 안의 `layer_id=3` 또는 L3 slug 존재로만 판단한다.
- 답변 본문이 역할/프로젝트를 잘못 말했더라도 provenance가 정상이면 "프롬프트 미적용"으로 단정하지 말고 "프롬프트는 적용됐으나 모델 응답이 고정 정체성 문구와 충돌"로 보고한다.
- 보고 형식: 세션 ID, KST 기준 최신 provenance 시각, workspace, role, system_prompt_chars, L1~L5 적용 여부, compile_error, 권장 조치를 표로 제시한다.$$,
    updated_at = NOW()
WHERE slug = 'intent-status-check'
  AND layer_id = 4
  AND content NOT LIKE '%프롬프트 적용 상태 조회%';

COMMIT;
