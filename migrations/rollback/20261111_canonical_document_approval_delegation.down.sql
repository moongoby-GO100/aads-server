-- 되돌림 스크립트 (수동 실행용)
-- 대응 마이그레이션: migrations/20261111_canonical_document_approval_delegation.sql
BEGIN;
DROP TABLE IF EXISTS project_document_approval_delegations;
ALTER TABLE project_document_events DROP COLUMN IF EXISTS metadata;
COMMIT;
