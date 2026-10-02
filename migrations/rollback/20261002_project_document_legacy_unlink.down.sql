-- Down for migrations/20261002_project_document_legacy_unlink.sql
-- Restores the original fully append-only trigger on legacy_links.
-- The events.goal_document_id column and the widened action CHECK are left in
-- place on purpose: events are append-only and may already hold
-- 'legacy_unlinked' rows, which a narrower CHECK would reject.
DROP TRIGGER IF EXISTS project_document_legacy_links_immutable ON project_document_legacy_links;
CREATE TRIGGER project_document_legacy_links_immutable BEFORE UPDATE OR DELETE
    ON project_document_legacy_links FOR EACH ROW EXECUTE FUNCTION reject_project_document_history_mutation();
DROP FUNCTION IF EXISTS guard_project_document_legacy_link_mutation();
