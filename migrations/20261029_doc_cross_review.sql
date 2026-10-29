BEGIN;
ALTER TABLE runner_model_config DROP CONSTRAINT IF EXISTS runner_model_config_size_chk;
ALTER TABLE runner_model_config ADD CONSTRAINT runner_model_config_size_chk
 CHECK(size IN ('XS','S','M','L','XL','AI_REVIEW','AI_REVIEW_LIGHT','AI_REVIEW_STD','AI_REVIEW_HEAVY'));
INSERT INTO runner_model_config(size,models,updated_at,updated_by)
SELECT sizes.size,c.models,now(),'migration'
FROM runner_model_config c CROSS JOIN (VALUES ('AI_REVIEW_LIGHT'),('AI_REVIEW_STD'),('AI_REVIEW_HEAVY')) sizes(size)
WHERE c.size='AI_REVIEW' ON CONFLICT(size) DO NOTHING;
CREATE TABLE IF NOT EXISTS document_cross_review_budget (
 tenant_id uuid NOT NULL, revision_id uuid NOT NULL, review_day date NOT NULL,
 PRIMARY KEY(revision_id,review_day)
);
CREATE INDEX IF NOT EXISTS document_cross_review_budget_day ON document_cross_review_budget(tenant_id,review_day);
CREATE TABLE IF NOT EXISTS document_cross_review_leases (
 revision_id uuid PRIMARY KEY, owner uuid NOT NULL, expires_at timestamptz NOT NULL
);
COMMIT;
