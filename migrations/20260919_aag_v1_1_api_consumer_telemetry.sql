-- AAG v1.1 V11-5: v1/v2 consumer telemetry for evidence-based cutover.
-- Additive and rollback-safe: v1 tables and APIs remain untouched.

CREATE TABLE IF NOT EXISTS aag_api_consumer_events (
    id BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL DEFAULT '',
    consumer TEXT NOT NULL,
    api_version TEXT NOT NULL CHECK (api_version IN ('v1','v2')),
    endpoint TEXT NOT NULL,
    project TEXT NOT NULL,
    repository_id TEXT,
    target_ref TEXT,
    snapshot_id UUID REFERENCES aag_graph_snapshots_v2(id),
    outcome TEXT NOT NULL,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aag_api_consumer_events_project_created
    ON aag_api_consumer_events(project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_aag_api_consumer_events_version_consumer
    ON aag_api_consumer_events(api_version, consumer, created_at DESC);

COMMENT ON TABLE aag_api_consumer_events IS
    'V11-5 v1/v2 read-consumer telemetry; v1 can retire only after residual use is zero';
