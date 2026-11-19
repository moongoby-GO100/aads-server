-- 대상: runner_model_config 의 XS 모델 목록 중 antigravity-flash 포함 행.
-- 조건: 해당 모델 포함 시에만 갱신하며 재실행은 멱등이다.
-- 금지: 다른 size 변경, 승인 없는 모델 복원 및 승인 게이트 우회 금지.
-- 롤백: 아래 기존 Rollback 절의 PUT /settings/runner-models 절차로 복원한다.
-- 183: Replace the unsupported XS runner model with the S primary model.
-- Rollback: use PUT /settings/runner-models to restore XS to
-- ["antigravity-flash", "claude-haiku", "codex:gpt-6-luna",
--  "gemini-3.8-flash-medium", "claude-sonnet-5-5", "codex:gpt-6.1-sol"].

UPDATE runner_model_config
SET models = '["codex:gpt-6.1-terra", "codex:gpt-6-luna", "claude-haiku", "gemini-3.8-flash-medium", "claude-sonnet-5-5", "codex:gpt-6.1-sol"]'::jsonb,
    updated_at = NOW(),
    updated_by = 'migration:runner-xs-drop-antigravity-flash'
WHERE size = 'XS'
  AND models ? 'antigravity-flash';
