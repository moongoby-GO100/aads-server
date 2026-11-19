-- 대상: runner_model_config 의 XS 모델 목록 중 antigravity-flash 포함 행.
-- 조건: 해당 모델 포함 시에만 갱신하며 재실행은 멱등이다.
-- 금지: 다른 size 변경, 승인 없는 모델 복원 및 승인 게이트 우회 금지.
-- 롤백(antigravity-flash 복원)의 승인·실행 주체: CEO 승인 후 운영 세션.
-- 적용 전 증거: scripts/apply_release_migrations.sh 및 release_migration_precheck.py 는 XS 이전 값을 저장하지 않는다.
-- 운영 세션이 적용 직전 SELECT size, models, updated_at, updated_by FROM runner_model_config WHERE size = 'XS';
-- 결과 전체를 핸드오버 DB에 기록하고 다시 읽어 확인한다. 적용 직후 같은 SELECT 결과도 기록한다.
-- 롤백: 복원 직전 같은 SELECT 로 현재 값이 적용 직후 기록값과 모두 같은지 확인한다.
-- 다르거나 기록이 없으면 중단하고 CEO 재확인을 받는다. 문서의 고정 목록으로 복원하지 않는다.
-- CEO 승인 후 운영 세션이 PUT /settings/runner-models 로 적용 전 기록의 XS models 를 복원하고 SELECT 로 확인한다.
-- 잠금·트랜잭션: 이 SQL 자체에는 lock_timeout, statement_timeout, BEGIN/COMMIT 이 없다.
-- scripts/apply_migration.sh 가 BEGIN/COMMIT 으로 감싸 실행한다. API 복원에는 이 SQL의 트랜잭션이 적용되지 않는다.
-- 183: Replace the unsupported XS runner model with the S primary model.

UPDATE runner_model_config
SET models = '["codex:gpt-6.1-terra", "codex:gpt-6-luna", "claude-haiku", "gemini-3.8-flash-medium", "claude-sonnet-5-5", "codex:gpt-6.1-sol"]'::jsonb,
    updated_at = NOW(),
    updated_by = 'migration:runner-xs-drop-antigravity-flash'
WHERE size = 'XS'
  AND models ? 'antigravity-flash';
