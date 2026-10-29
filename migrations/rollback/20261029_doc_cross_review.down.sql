BEGIN;
-- Preserve every current row, including operator edits, before removing cross-review sizes.
CREATE TABLE IF NOT EXISTS runner_model_config_cross_review_rollback_backup
 (LIKE runner_model_config INCLUDING DEFAULTS);
INSERT INTO runner_model_config_cross_review_rollback_backup
 SELECT * FROM runner_model_config WHERE size IN ('AI_REVIEW_LIGHT','AI_REVIEW_STD','AI_REVIEW_HEAVY');
DELETE FROM runner_model_config WHERE size IN ('AI_REVIEW_LIGHT','AI_REVIEW_STD','AI_REVIEW_HEAVY');
ALTER TABLE runner_model_config DROP CONSTRAINT IF EXISTS runner_model_config_size_chk;
-- 090 CHECK를 쓰는 환경은 090의 정의를 수동 재적용.
-- 테이블 보존 — DROP 금지 규칙, 수동 정리는 별도 승인.
COMMIT;
