-- Application rollback leaves the additive registry and cleanup evidence intact.
-- Do not drop pending cleanup records: a delayed PC command may still create a tab.
-- Revert the application release through the normal routing rollback gate.
BEGIN;
SELECT count(*) AS preserved_bindings FROM gemini_web_usage_bindings;
SELECT count(*) AS preserved_cleanup_intents FROM gemini_web_usage_cleanup;
COMMIT;
-- Keep fencing tokens and unresolved bootstrap evidence during code rollback.
SELECT lane, kind, state, expires_at FROM gemini_web_usage_lease;
