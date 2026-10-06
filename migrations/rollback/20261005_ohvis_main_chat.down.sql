-- Rollback keeps nothing that the feature owns. Stop automatic effects first (unset OHVIS_MAIN_CHAT_AUTO_EFFECT)
-- and reconcile any outbox row in state 'unknown' against the runner BEFORE running this: dropping the outbox
-- loses the only record linking an effect key to a remote job.
BEGIN;
DROP TABLE IF EXISTS ohvis_main_chat_notices;
DROP TABLE IF EXISTS ohvis_action_outbox;
DROP TABLE IF EXISTS ohvis_report_reviews;
DROP TABLE IF EXISTS ohvis_action_grants;
DROP TABLE IF EXISTS ohvis_report_inbox;
DROP TABLE IF EXISTS ohvis_main_chat_control;
DROP TABLE IF EXISTS ohvis_main_chat_routes;
DROP TABLE IF EXISTS ohvis_main_chat_audit;
DROP FUNCTION IF EXISTS ohvis_main_chat_audit_append_only();
COMMIT;
