-- 163: OHVIS internal LangSmith-compatible LLMOps foundation (v1).
-- Date: 2026-09-08
-- PRD: docs/reports/20260908_langsmith_self_hosted_ohvis_prd.md
--
-- Additive only. No DROP, no TRUNCATE, no ALTER that removes or retypes a
-- column, no data mutation of existing tables. Re-running this file must be a
-- no-op (every statement is IF NOT EXISTS / CREATE OR REPLACE).
--
-- Backward compatibility: migration 158's `ohvis_harness_traces` keeps
-- receiving writes from goal_manager / pipeline_runner_service / collector
-- harnesses. It is NOT copied or migrated. The `llmops_trace_unified` view
-- exposes both ledgers under one read contract so the LLMOps API can serve
-- legacy rows without a backfill.

-- ── trace ledger ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS llmops_traces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL UNIQUE,
    graph_run_id TEXT,
    project TEXT,
    session_id UUID,
    ohvis_task_id UUID REFERENCES ohvis_tasks(id) ON DELETE SET NULL,
    task_ref TEXT,
    source TEXT NOT NULL DEFAULT 'internal',
    run_type TEXT NOT NULL DEFAULT 'chain',
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    error TEXT,
    cost_usd NUMERIC(12, 6),
    latency_ms INTEGER,
    quality_score DOUBLE PRECISION,
    tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_project_created
    ON llmops_traces (project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_graph_run
    ON llmops_traces (graph_run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_status
    ON llmops_traces (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_session
    ON llmops_traces (session_id, created_at DESC);

-- span_id/parent_span_id stay TEXT with no FK to llmops_traces so that spans
-- may also be attached to a legacy ohvis_harness_traces run id.
CREATE TABLE IF NOT EXISTS llmops_spans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    span_type TEXT NOT NULL DEFAULT 'chain',
    name TEXT NOT NULL DEFAULT '',
    model_id TEXT,
    status TEXT NOT NULL DEFAULT 'success',
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    error TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_usd NUMERIC(12, 6),
    latency_ms INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_llmops_spans_trace_span
    ON llmops_spans (trace_id, span_id);

CREATE INDEX IF NOT EXISTS idx_llmops_spans_trace_started
    ON llmops_spans (trace_id, started_at);

CREATE TABLE IF NOT EXISTS llmops_tool_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL,
    span_id TEXT,
    tool_name TEXT NOT NULL,
    risk_tier TEXT NOT NULL DEFAULT 'read',
    approval_state TEXT NOT NULL DEFAULT 'not_required',
    status TEXT NOT NULL DEFAULT 'success',
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    error TEXT,
    latency_ms INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_tool_calls_trace
    ON llmops_tool_calls (trace_id, created_at);

CREATE INDEX IF NOT EXISTS idx_llmops_tool_calls_tool
    ON llmops_tool_calls (tool_name, created_at DESC);

-- ── evaluation ledger ─────────────────────────────────────────────────────

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
    input TEXT NOT NULL DEFAULT '',
    expected TEXT NOT NULL DEFAULT '',
    rubric JSONB NOT NULL DEFAULT '{}'::JSONB,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One example per (dataset, source trace): promoting the same failed trace
-- twice must not duplicate the eval set.
CREATE UNIQUE INDEX IF NOT EXISTS idx_llmops_examples_dataset_trace
    ON llmops_examples (dataset_id, source_trace_id)
    WHERE source_trace_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llmops_examples_dataset
    ON llmops_examples (dataset_id, created_at DESC);

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
    trace_id TEXT,
    evaluator TEXT NOT NULL DEFAULT 'rule_v1',
    evaluator_version TEXT NOT NULL DEFAULT 'v1',
    key TEXT NOT NULL DEFAULT 'overall',
    score DOUBLE PRECISION,
    passed BOOLEAN,
    comment TEXT NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_scores_experiment
    ON llmops_scores (experiment_id, key);

CREATE INDEX IF NOT EXISTS idx_llmops_scores_example
    ON llmops_scores (example_id, created_at DESC);

CREATE TABLE IF NOT EXISTS llmops_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL,
    rating INTEGER,
    label TEXT,
    comment TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_feedback_trace
    ON llmops_feedback (trace_id, created_at DESC);

-- ── backward-compatible read contract ─────────────────────────────────────
-- `llmops_trace_unified` is created only when migration 158 has been applied.
-- Legacy rows keep their own storage; they are surfaced with a synthetic
-- addressable trace id (`harness:<uuid>`) when the legacy writer left
-- trace_id NULL. No existing row is rewritten.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'ohvis_harness_traces'
    ) THEN
        EXECUTE $view$
            CREATE OR REPLACE VIEW llmops_trace_unified AS
            SELECT
                t.trace_id                                   AS trace_id,
                t.graph_run_id                               AS graph_run_id,
                t.project                                    AS project,
                t.session_id                                 AS session_id,
                t.ohvis_task_id                              AS ohvis_task_id,
                t.run_type                                   AS run_type,
                t.name                                       AS name,
                t.status                                     AS status,
                t.input_summary                              AS input_summary,
                t.output_summary                             AS output_summary,
                t.error                                      AS error,
                t.cost_usd                                   AS cost_usd,
                t.latency_ms                                 AS latency_ms,
                t.quality_score                              AS quality_score,
                t.metadata                                   AS metadata,
                t.created_at                                 AS created_at,
                'llmops'::TEXT                               AS ledger
            FROM llmops_traces t
            UNION ALL
            SELECT
                COALESCE(NULLIF(h.trace_id, ''), 'harness:' || h.id::TEXT),
                h.graph_run_id,
                h.project,
                h.session_id,
                h.ohvis_task_id,
                h.run_type,
                COALESCE(NULLIF(h.run_type, ''), 'chain'),
                CASE WHEN h.error IS NULL OR h.error = '' THEN 'success' ELSE 'error' END,
                h.input_summary,
                h.output_summary,
                h.error,
                h.cost_usd,
                h.latency_ms,
                NULL::DOUBLE PRECISION,
                h.metadata,
                h.created_at,
                'harness'::TEXT
            FROM ohvis_harness_traces h
        $view$;
    END IF;
END
$$;
