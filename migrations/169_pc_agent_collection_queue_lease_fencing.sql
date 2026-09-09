-- FOOD-PC-QUEUE-CANONICAL-DB-P0-20260909-R5
-- Additive lease fencing for the PostgreSQL-authoritative PC Agent queue.

ALTER TABLE pc_agent_collection_queue
    ADD COLUMN IF NOT EXISTS owner_instance TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS owner_epoch BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_pc_agent_collection_queue_lease_expiry
    ON pc_agent_collection_queue(lease_expires_at)
    WHERE status = 'running';

-- Operational read-only checks:
-- SELECT status, count(*) FROM pc_agent_collection_queue GROUP BY status;
-- SELECT id, owner_instance, owner_epoch, lease_expires_at
--   FROM pc_agent_collection_queue WHERE status = 'running' ORDER BY lease_expires_at;
