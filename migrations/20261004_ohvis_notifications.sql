-- OHVIS in-app notifications (inbox rows only). No external channel is involved: nothing here sends anything.
-- Additive only. Rows are keyed by (tenant, recipient, dedupe_key) so a replayed event never notifies twice.
-- review/revision/session ids are plain uuids (no FK) so chat cleanup never blocks the inbox or the audit trail.
BEGIN;

CREATE TABLE IF NOT EXISTS ohvis_notifications (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    recipient_user_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN
        ('review_requested','change_received','re_reported','approval_waiting','approved','revoked','failed')),
    review_id uuid,
    revision_id uuid,
    revision integer,
    session_id uuid,
    event_id bigint,
    title text NOT NULL,
    body text NOT NULL DEFAULT '',
    link text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at timestamptz,
    UNIQUE (tenant_id, recipient_user_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS ohvis_notifications_inbox_idx
    ON ohvis_notifications(tenant_id, recipient_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ohvis_notifications_unread_idx
    ON ohvis_notifications(tenant_id, recipient_user_id) WHERE read_at IS NULL;

COMMIT;
