BEGIN;
-- Preserve every current row, including operator edits, before restoring the 090 CHECK.
CREATE TABLE IF NOT EXISTS runner_model_config_cross_review_rollback_backup
 (LIKE runner_model_config INCLUDING DEFAULTS);
INSERT INTO runner_model_config_cross_review_rollback_backup
 SELECT * FROM runner_model_config WHERE size IN ('AI_REVIEW_LIGHT','AI_REVIEW_STD','AI_REVIEW_HEAVY');
DELETE FROM runner_model_config WHERE size IN ('AI_REVIEW_LIGHT','AI_REVIEW_STD','AI_REVIEW_HEAVY');
ALTER TABLE runner_model_config DROP CONSTRAINT IF EXISTS runner_model_config_size_chk;
ALTER TABLE runner_model_config ADD CONSTRAINT runner_model_config_size_chk
 CHECK(size IN ('XS','S','M','L','XL','AI_REVIEW'));
DROP TABLE IF EXISTS document_cross_review_budget;
DROP TABLE IF EXISTS document_cross_review_leases;
COMMIT;
