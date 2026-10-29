BEGIN;
ALTER TABLE project_document_revisions
  ADD COLUMN IF NOT EXISTS doc_tier text CHECK (doc_tier IN ('D1','D2','D3','SPLIT')),
  ADD COLUMN IF NOT EXISTS author_model text,
  ADD COLUMN IF NOT EXISTS author_provider text;
CREATE TABLE IF NOT EXISTS project_document_reviews (
  id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  project_key text NOT NULL,
  head_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  reviewer_model text NOT NULL,
  reviewer_provider text NOT NULL,
  role text NOT NULL CHECK (role IN ('cross','same_company_other_model','code')),
  cross_company boolean NOT NULL,
  verdict text NOT NULL CHECK (verdict IN ('pass','reject','blocked','attempt_failed')),
  reason text,
  override_by text, override_reason text,
  source_task_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (head_id, revision_id) REFERENCES project_document_revisions(head_id, id)
);
CREATE INDEX IF NOT EXISTS project_document_reviews_rev_idx ON project_document_reviews(revision_id, id);
DROP TRIGGER IF EXISTS project_document_reviews_immutable ON project_document_reviews;
CREATE TRIGGER project_document_reviews_immutable BEFORE UPDATE OR DELETE
  ON project_document_reviews FOR EACH ROW EXECUTE FUNCTION reject_project_document_history_mutation();
COMMIT;
