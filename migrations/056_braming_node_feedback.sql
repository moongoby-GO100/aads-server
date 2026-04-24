ALTER TABLE braming_nodes
    ADD COLUMN IF NOT EXISTS ceo_opinion TEXT,
    ADD COLUMN IF NOT EXISTS ceo_opinion_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS picked BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS picked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS picked_by TEXT;

UPDATE braming_nodes
SET picked = TRUE,
    picked_at = COALESCE(picked_at, created_at)
WHERE COALESCE((metadata ->> 'picked')::boolean, FALSE) = TRUE;

CREATE TABLE IF NOT EXISTS braming_node_votes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES braming_sessions(id) ON DELETE CASCADE,
    node_id UUID NOT NULL REFERENCES braming_nodes(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    vote TEXT NOT NULL CHECK (vote IN ('up', 'down')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (node_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_braming_node_votes_session
    ON braming_node_votes(session_id);

CREATE INDEX IF NOT EXISTS idx_braming_node_votes_node
    ON braming_node_votes(node_id);
