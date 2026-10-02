-- AADS-PROJECT-DOCUMENTS-M3-LEGACY-UNLINK-API-20261002
-- Additive: lets DELETE /projects/{key}/documents/{doc}/legacy-links/{goal_document_id}
-- remove a legacy link and audit it. Idempotent; no goal_documents row is touched.
--
-- 1) events: 'legacy_unlinked' action + which goal_document_id was unlinked.
-- 2) legacy_links: DELETE is allowed ONLY inside a transaction that set
--    aads.legacy_unlink='on' (the API does this). UPDATE and every other DELETE
--    stay rejected, so ad hoc SQL cannot erase link history.

ALTER TABLE project_document_events ADD COLUMN IF NOT EXISTS goal_document_id bigint;

DO $$
DECLARE c text;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid='project_document_events'::regclass AND contype='c'
               AND pg_get_constraintdef(oid) LIKE '%action%'
    LOOP
        EXECUTE format('ALTER TABLE project_document_events DROP CONSTRAINT %I', c);
    END LOOP;
    ALTER TABLE project_document_events ADD CONSTRAINT project_document_events_action_check
        CHECK (action IN ('created','revised','review','approved','archived','goal_linked',
                          'legacy_linked','legacy_unlinked'));
END $$;

CREATE OR REPLACE FUNCTION guard_project_document_legacy_link_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP = 'DELETE' AND current_setting('aads.legacy_unlink', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'project document history is append-only';
END $$;

DROP TRIGGER IF EXISTS project_document_legacy_links_immutable ON project_document_legacy_links;
CREATE TRIGGER project_document_legacy_links_immutable BEFORE UPDATE OR DELETE
    ON project_document_legacy_links FOR EACH ROW
    EXECUTE FUNCTION guard_project_document_legacy_link_mutation();
