BEGIN;
DROP TABLE IF EXISTS project_document_reviews;
ALTER TABLE project_document_revisions
  DROP COLUMN IF EXISTS doc_tier,
  DROP COLUMN IF EXISTS author_model,
  DROP COLUMN IF EXISTS author_provider;
COMMIT;
