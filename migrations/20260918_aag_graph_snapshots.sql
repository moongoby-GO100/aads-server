CREATE TABLE IF NOT EXISTS aag_graph_snapshots (
    id BIGSERIAL PRIMARY KEY,
    project TEXT NOT NULL,
    host TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    commit_sha TEXT,
    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    unresolved JSONB NOT NULL DEFAULT '[]'::jsonb,
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    finding_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project, generated_at)
);

CREATE INDEX IF NOT EXISTS idx_aag_graph_snapshots_project_created_at
    ON aag_graph_snapshots (project, created_at DESC);
