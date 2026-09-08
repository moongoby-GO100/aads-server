-- 163: OHVIS internal LangSmith-compatible LLMOps foundation.
-- Date: 2026-09-08
-- Task: AADS-LANGSMITH-INTERNAL-LLMOPS-P0
--
-- This migration is ADDITIVE ONLY. It contains no DROP, TRUNCATE, DELETE, or
-- ALTER ... DROP statement, and it is safe to re-run (every object is guarded by
-- IF NOT EXISTS or an existence check).
--
-- The existing `ohvis_harness_traces` table (migration 158) is NOT retired.
-- It stays the v1 write path; `llmops_traces` is the v2 canonical ledger and
-- `llmops_traces_compat` unions both so readers never lose history.

-- ── trace / span / tool call ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS llmops_traces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL UNIQUE,
    trace_key TEXT,
    graph_run_id TEXT NOT NULL,
    project TEXT,
    session_id UUID,
    ohvis_task_id UUID,
    source TEXT NOT NULL DEFAULT 'internal',
    run_type TEXT NOT NULL DEFAULT 'chain',
    status TEXT NOT NULL DEFAULT 'success',
    model TEXT,
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER,
    cost_usd NUMERIC(12, 6),
    quality_score DOUBLE PRECISION,
    error TEXT,
    error_class TEXT,
    external_trace_id TEXT,
    tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- trace_key lets a mirrored/retried write be idempotent without a DELETE path.
CREATE UNIQUE INDEX IF NOT EXISTS idx_llmops_traces_trace_key
    ON llmops_traces (trace_key) WHERE trace_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llmops_traces_project_created
    ON llmops_traces (project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_graph_run
    ON llmops_traces (graph_run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_status_created
    ON llmops_traces (status, created_at DESC);

CREATE TABLE IF NOT EXISTS llmops_spans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- TEXT keeps compatibility with the earlier additive migration and with
    -- external/non-UUID trace identifiers. The service stores the canonical
    -- llmops_traces.id UUID as text for internal spans.
    trace_id TEXT NOT NULL,
    parent_span_id UUID REFERENCES llmops_spans(id) ON DELETE CASCADE,
    span_type TEXT NOT NULL DEFAULT 'chain',
    name TEXT NOT NULL DEFAULT '',
    sequence INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    model TEXT,
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER,
    cost_usd NUMERIC(12, 6),
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_llmops_spans_trace
    ON llmops_spans (trace_id, sequence, started_at);

CREATE TABLE IF NOT EXISTS llmops_tool_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL,
    span_id UUID REFERENCES llmops_spans(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    risk_tier TEXT NOT NULL DEFAULT 'read',
    approval_state TEXT NOT NULL DEFAULT 'not_required',
    status TEXT NOT NULL DEFAULT 'success',
    sequence INTEGER NOT NULL DEFAULT 0,
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER,
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_tool_calls_trace
    ON llmops_tool_calls (trace_id, sequence);

CREATE INDEX IF NOT EXISTS idx_llmops_tool_calls_tool
    ON llmops_tool_calls (tool_name, created_at DESC);

-- ── dataset / example ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS llmops_datasets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE,
    project TEXT,
    title TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    source_filter JSONB NOT NULL DEFAULT '{}'::JSONB,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_datasets_project
    ON llmops_datasets (project, updated_at DESC);

CREATE TABLE IF NOT EXISTS llmops_examples (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL REFERENCES llmops_datasets(id) ON DELETE CASCADE,
    source_trace_id TEXT,
    source_ref TEXT,
    input TEXT NOT NULL DEFAULT '',
    expected TEXT NOT NULL DEFAULT '',
    rubric JSONB NOT NULL DEFAULT '{}'::JSONB,
    tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Promoting the same trace twice must not duplicate the example.
CREATE UNIQUE INDEX IF NOT EXISTS idx_llmops_examples_dataset_source
    ON llmops_examples (dataset_id, source_ref) WHERE source_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llmops_examples_dataset
    ON llmops_examples (dataset_id, created_at DESC);

-- ── experiment / score / feedback ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS llmops_experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL REFERENCES llmops_datasets(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    evaluator TEXT NOT NULL DEFAULT 'rule_v1',
    evaluator_version TEXT NOT NULL DEFAULT 'v1',
    candidate_sha TEXT,
    model_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_by TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_llmops_experiments_dataset
    ON llmops_experiments (dataset_id, started_at DESC);

CREATE TABLE IF NOT EXISTS llmops_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id UUID NOT NULL REFERENCES llmops_experiments(id) ON DELETE CASCADE,
    example_id UUID REFERENCES llmops_examples(id) ON DELETE CASCADE,
    source_trace_id TEXT,
    evaluator TEXT NOT NULL DEFAULT 'rule_v1',
    evaluator_version TEXT NOT NULL DEFAULT 'v1',
    criterion TEXT NOT NULL DEFAULT 'overall',
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    passed BOOLEAN,
    comment TEXT NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_scores_experiment
    ON llmops_scores (experiment_id, criterion);

CREATE TABLE IF NOT EXISTS llmops_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT,
    source_ref TEXT,
    rating INTEGER,
    label TEXT,
    comment TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_feedback_trace
    ON llmops_feedback (trace_id, created_at DESC);

-- ── v1 compatibility view ───────────────────────────────────────────────────
-- Unions the v2 ledger with the existing harness traces so no reader loses the
-- 100+ rows already recorded by migration 158. Created only when absent, so the
-- migration never has to DROP an existing view.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.views
        WHERE table_schema = 'public' AND table_name = 'llmops_traces_compat'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'ohvis_harness_traces'
    ) THEN
        EXECUTE $view$
            CREATE VIEW llmops_traces_compat AS
            SELECT
                t.id,
                'llmops_traces'::TEXT AS source_table,
                t.graph_run_id,
                t.project,
                t.session_id,
                t.ohvis_task_id,
                t.run_type,
                t.status,
                t.input_summary,
                t.output_summary,
                t.latency_ms,
                t.cost_usd,
                t.error,
                t.created_at
            FROM llmops_traces t
            UNION ALL
            SELECT
                h.id,
                'ohvis_harness_traces'::TEXT AS source_table,
                h.graph_run_id,
                h.project,
                h.session_id,
                h.ohvis_task_id,
                h.run_type,
                CASE WHEN h.error IS NULL OR h.error = '' THEN 'success' ELSE 'error' END AS status,
                h.input_summary,
                h.output_summary,
                h.latency_ms,
                h.cost_usd,
                h.error,
                h.created_at
            FROM ohvis_harness_traces h
        $view$;
    END IF;
END
$$;

-- ── seed: default failure dataset ───────────────────────────────────────────

INSERT INTO llmops_datasets (slug, project, title, purpose, source_filter, metadata)
VALUES (
    'aads-failed-traces',
    'AADS',
    'AADS failed and low-quality traces',
    'Traces that errored or scored below the quality floor, promoted for offline rule evaluation.',
    '{"status": ["error"], "quality_score_lt": 0.4}'::JSONB,
    '{"owner": "ohvis-llmops", "seeded_by": "migration-163"}'::JSONB
)
ON CONFLICT (slug) DO UPDATE
SET title = EXCLUDED.title,
    purpose = EXCLUDED.purpose,
    source_filter = EXCLUDED.source_filter,
    metadata = EXCLUDED.metadata,
    enabled = TRUE,
    updated_at = NOW();
