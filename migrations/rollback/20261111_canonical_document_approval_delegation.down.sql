BEGIN;
DROP TABLE IF EXISTS project_document_approval_delegations;
ALTER TABLE project_document_events DROP COLUMN IF EXISTS metadata;
COMMIT;
