-- 163: OHVIS internal LangSmith-compatible LLMOps foundation.
-- Date: 2026-09-08
-- Task: AADS-LANGSMITH-INTERNAL-LLMOPS-P0
--
-- This migration is ADDITIVE ONLY. It contains no DROP, TRUNCATE, DELETE, or
-- ALTER ... DROP statement, and every object is created with IF NOT EXISTS so
-- the file is idempotent and safe to re-apply.
--
-- Existing `ohvis_harness_traces` (migration 158) is NOT modified and NOT
-- migrated. It stays the v1 ledger; `llmops_trace_compat` unions v1 rows with
-- the new v2 `llmops_traces` so trace readers see both without a backfill.

-- ---------------------------------------------------------------------------
-- Trace / span / tool call
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS llmops_traces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL UNIQUE,
    graph_run_id TEXT,
    project TEXT,
    session_id UUID,
    ohvis_task_id UUID REFERENCES ohvis_tasks(id) ON DELETE SET NULL,
    task_ref TEXT,
    provider TEXT NOT NULL DEFAULT 'internal',
    run_type TEXT NOT NULL DEFAULT 'chain',
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER,
    cost_usd NUMERIC(12, 6),
    quality_score DOUBLE PRECISION,
    error TEXT,
    error_class TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_project_created
    ON llmops_traces (project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_graph_run
    ON llmops_traces (graph_run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_session
    ON llmops_traces (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_status
    ON llmops_traces (status, created_at DESC);

CREATE TABLE IF NOT EXISTS llmops_spans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id UUID NOT NULL REFERENCES llmops_traces(id) ON DELETE CASCADE,
    parent_span_id UUID REFERENCES llmops_spans(id) ON DELETE CASCADE,
    span_type TEXT NOT NULL DEFAULT 'chain',
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    model_id TEXT,
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    latency_ms INTEGER,
    cost_usd NUMERIC(12, 6),
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_spans_trace
    ON llmops_spans (trace_id, started_at);

CREATE INDEX IF NOT EXISTS idx_llmops_spans_parent
    ON llmops_spans (parent_span_id);

CREATE TABLE IF NOT EXISTS llmops_tool_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id UUID NOT NULL REFERENCES llmops_traces(id) ON DELETE CASCADE,
    span_id UUID REFERENCES llmops_spans(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    risk_tier TEXT NOT NULL DEFAULT 'read',
    approval_state TEXT NOT NULL DEFAULT 'not_required',
    status TEXT NOT NULL DEFAULT 'ok',
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER,
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_tool_calls_trace
    ON llmops_tool_calls (trace_id, created_at);

CREATE INDEX IF NOT EXISTS idx_llmops_tool_calls_tool
    ON llmops_tool_calls (tool_name, created_at DESC);

-- ---------------------------------------------------------------------------
-- Dataset / example / experiment / score
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS llmops_datasets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    project TEXT,
    purpose TEXT NOT NULL DEFAULT 'regression',
    description TEXT NOT NULL DEFAULT '',
    source_filter JSONB NOT NULL DEFAULT '{}'::JSONB,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_datasets_project
    ON llmops_datasets (project, enabled);

CREATE TABLE IF NOT EXISTS llmops_examples (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL REFERENCES llmops_datasets(id) ON DELETE CASCADE,
    source_trace_id UUID REFERENCES llmops_traces(id) ON DELETE SET NULL,
    -- Free-form provenance pointer. Holds a v2 trace_id or a v1
    -- ohvis_harness_traces UUID so legacy traces can be promoted too.
    source_ref TEXT,
    input TEXT NOT NULL DEFAULT '',
    expected TEXT NOT NULL DEFAULT '',
    rubric JSONB NOT NULL DEFAULT '[]'::JSONB,
    tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_examples_dataset
    ON llmops_examples (dataset_id, created_at DESC);

-- Promotion of the same source trace into the same dataset is idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_llmops_examples_dataset_source
    ON llmops_examples (dataset_id, source_ref)
    WHERE source_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS llmops_experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL REFERENCES llmops_datasets(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    candidate_sha TEXT,
    model_id TEXT,
    evaluator TEXT NOT NULL DEFAULT 'rule',
    evaluator_version TEXT NOT NULL DEFAULT 'v1',
    status TEXT NOT NULL DEFAULT 'running',
    summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_experiments_dataset
    ON llmops_experiments (dataset_id, started_at DESC);

CREATE TABLE IF NOT EXISTS llmops_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id UUID NOT NULL REFERENCES llmops_experiments(id) ON DELETE CASCADE,
    example_id UUID REFERENCES llmops_examples(id) ON DELETE SET NULL,
    evaluator TEXT NOT NULL DEFAULT 'rule',
    evaluator_version TEXT NOT NULL DEFAULT 'v1',
    rule_key TEXT,
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    passed BOOLEAN,
    comment TEXT NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::JSONB,
    source_trace_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_scores_experiment
    ON llmops_scores (experiment_id, created_at);

CREATE INDEX IF NOT EXISTS idx_llmops_scores_example
    ON llmops_scores (example_id);

CREATE TABLE IF NOT EXISTS llmops_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id UUID REFERENCES llmops_traces(id) ON DELETE CASCADE,
    source_ref TEXT,
    rating INTEGER,
    label TEXT,
    comment TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'ceo',
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_feedback_trace
    ON llmops_feedback (trace_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- v1 compatibility
-- ---------------------------------------------------------------------------
-- Read-only union so trace listing sees migration-158 rows without copying
-- them. `ohvis_harness_traces` keeps receiving writes from
-- app/services/ohvis_harness_trace.py exactly as before.

CREATE OR REPLACE VIEW llmops_trace_compat AS
SELECT
    t.id                AS id,
    t.trace_id          AS trace_id,
    t.graph_run_id      AS graph_run_id,
    t.project           AS project,
    t.session_id        AS session_id,
    t.ohvis_task_id     AS ohvis_task_id,
    t.provider          AS provider,
    t.run_type          AS run_type,
    t.status            AS status,
    t.input_summary     AS input_summary,
    t.output_summary    AS output_summary,
    t.latency_ms        AS latency_ms,
    t.cost_usd          AS cost_usd,
    t.quality_score     AS quality_score,
    t.error             AS error,
    t.metadata          AS metadata,
    t.created_at        AS created_at,
    'v2'::TEXT          AS schema_version
FROM llmops_traces t
UNION ALL
SELECT
    h.id                                        AS id,
    COALESCE(h.trace_id, h.id::TEXT)            AS trace_id,
    h.graph_run_id                              AS graph_run_id,
    h.project                                   AS project,
    h.session_id                                AS session_id,
    h.ohvis_task_id                             AS ohvis_task_id,
    h.provider                                  AS provider,
    h.run_type                                  AS run_type,
    CASE WHEN h.error IS NULL OR h.error = '' THEN 'success' ELSE 'error' END AS status,
    h.input_summary                             AS input_summary,
    h.output_summary                            AS output_summary,
    h.latency_ms                                AS latency_ms,
    h.cost_usd                                  AS cost_usd,
    NULL::DOUBLE PRECISION                      AS quality_score,
    h.error                                     AS error,
    h.metadata                                  AS metadata,
    h.created_at                                AS created_at,
    'v1'::TEXT                                  AS schema_version
FROM ohvis_harness_traces h;

-- ---------------------------------------------------------------------------
-- Seed: default failure-regression dataset (idempotent)
-- ---------------------------------------------------------------------------

INSERT INTO llmops_datasets (slug, title, project, purpose, description, source_filter, metadata)
VALUES (
    'aads-failure-regression',
    'AADS failure regression',
    'AADS',
    'regression',
    'Failed or low-quality AADS traces promoted for offline rule evaluation.',
    '{"status": ["error", "failed"], "quality_score_lt": 0.4}'::JSONB,
    '{"owner": "ohvis-llmops", "seeded_by": "migration_163"}'::JSONB
)
ON CONFLICT (slug) DO UPDATE
SET title = EXCLUDED.title,
    project = EXCLUDED.project,
    purpose = EXCLUDED.purpose,
    description = EXCLUDED.description,
    source_filter = EXCLUDED.source_filter,
    enabled = TRUE,
    updated_at = NOW();
