-- Inbox rows are derived from mockup_review_events (kept); dropping them loses read state only.
BEGIN;
DROP TABLE IF EXISTS ohvis_notifications;
COMMIT;
