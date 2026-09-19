DROP INDEX IF EXISTS idx_goal_documents_versions;
DROP INDEX IF EXISTS uq_goal_documents_latest;

ALTER TABLE goal_documents DROP CONSTRAINT IF EXISTS goal_documents_no_self_supersede;
ALTER TABLE goal_documents DROP CONSTRAINT IF EXISTS goal_documents_version_check;
ALTER TABLE goal_documents DROP CONSTRAINT IF EXISTS goal_documents_status_check;

ALTER TABLE goal_documents
    DROP COLUMN IF EXISTS supersedes_id,
    DROP COLUMN IF EXISTS change_summary,
    DROP COLUMN IF EXISTS is_latest,
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS version,
    DROP COLUMN IF EXISTS document_key,
    DROP COLUMN IF EXISTS updated_at;
