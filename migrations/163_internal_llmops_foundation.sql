-- 163: OHVIS internal LangSmith-compatible LLMOps foundation.
-- Date: 2026-09-08
-- Task: AADS-LANGSMITH-INTERNAL-LLMOPS-P0
-- PRD: docs/reports/20260908_langsmith_self_hosted_ohvis_prd.md
--
-- Additive only. No DROP / TRUNCATE / DELETE / destructive ALTER.
-- Re-running this file must be a no-op (CREATE ... IF NOT EXISTS,
-- CREATE OR REPLACE VIEW, INSERT ... ON CONFLICT DO NOTHING).
--
-- Backward compatibility: ohvis_harness_traces (migration 158) stays the
-- source of truth for already-recorded rows. It is neither dropped nor
-- rewritten; llmops_legacy_traces exposes it in the new trace shape and
-- app/services/llmops_service.py falls back to it when llmops_traces is
-- missing or empty.

-- ── trace / span / tool call ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS llmops_traces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL UNIQUE,
    graph_run_id TEXT,
    project TEXT,
    session_id UUID,
    ohvis_task_id UUID,
    source TEXT NOT NULL DEFAULT 'internal',
    run_type TEXT NOT NULL DEFAULT 'chain',
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'success',
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    quality_score DOUBLE PRECISION,
    cost_usd NUMERIC(12, 6),
    latency_ms INTEGER,
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

CREATE INDEX IF NOT EXISTS idx_llmops_traces_status_created
    ON llmops_traces (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llmops_traces_session
    ON llmops_traces (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS llmops_spans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL REFERENCES llmops_traces(trace_id) ON DELETE CASCADE,
    parent_span_id UUID REFERENCES llmops_spans(id) ON DELETE CASCADE,
    span_type TEXT NOT NULL DEFAULT 'chain',
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'success',
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    model_id TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_usd NUMERIC(12, 6),
    latency_ms INTEGER,
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_llmops_spans_trace
    ON llmops_spans (trace_id, started_at);

CREATE TABLE IF NOT EXISTS llmops_tool_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL REFERENCES llmops_traces(trace_id) ON DELETE CASCADE,
    span_id UUID REFERENCES llmops_spans(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    risk_tier TEXT NOT NULL DEFAULT 'read',
    approval_state TEXT NOT NULL DEFAULT 'not_required',
    status TEXT NOT NULL DEFAULT 'success',
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

-- ── dataset / example / experiment / score ──────────────────────────────────

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

CREATE INDEX IF NOT EXISTS idx_llmops_examples_dataset
    ON llmops_examples (dataset_id, created_at DESC);

-- 같은 trace를 같은 dataset에 두 번 승격하지 않는다 (수동 example은 제외).
CREATE UNIQUE INDEX IF NOT EXISTS idx_llmops_examples_dataset_trace
    ON llmops_examples (dataset_id, source_trace_id)
    WHERE source_trace_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS llmops_experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL REFERENCES llmops_datasets(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    candidate_sha TEXT,
    model_id TEXT,
    evaluator TEXT NOT NULL DEFAULT 'rule',
    evaluator_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_llmops_experiments_dataset
    ON llmops_experiments (dataset_id, started_at DESC);

CREATE TABLE IF NOT EXISTS llmops_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id UUID NOT NULL REFERENCES llmops_experiments(id) ON DELETE CASCADE,
    example_id UUID REFERENCES llmops_examples(id) ON DELETE SET NULL,
    source_trace_id TEXT,
    evaluator TEXT NOT NULL DEFAULT 'rule',
    evaluator_version TEXT NOT NULL DEFAULT '',
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    passed BOOLEAN NOT NULL DEFAULT FALSE,
    comment TEXT NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_scores_experiment
    ON llmops_scores (experiment_id, created_at);

-- ── feedback ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS llmops_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL,
    rating INTEGER,
    label TEXT,
    comment TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'ceo',
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llmops_feedback_trace
    ON llmops_feedback (trace_id, created_at DESC);

-- ── ohvis_harness_traces v1 compatibility view ──────────────────────────────
-- 기존 102건+ 하네스 trace를 신규 trace 형태로 읽기 전용 노출한다.
-- 기존 테이블/API는 그대로 유지되며 backfill INSERT는 하지 않는다.

CREATE OR REPLACE VIEW llmops_legacy_traces AS
SELECT
    COALESCE(NULLIF(h.trace_id, ''), h.id::TEXT) AS trace_id,
    h.graph_run_id,
    h.project,
    h.session_id,
    h.ohvis_task_id,
    'ohvis_harness_traces'::TEXT AS source,
    h.run_type,
    COALESCE(NULLIF(h.metadata->>'component', ''), h.run_type) AS name,
    CASE WHEN h.error IS NULL OR h.error = '' THEN 'success' ELSE 'error' END AS status,
    h.input_summary,
    h.output_summary,
    NULL::DOUBLE PRECISION AS quality_score,
    h.cost_usd,
    h.latency_ms,
    h.error,
    NULL::TEXT AS error_class,
    h.metadata,
    h.tool_calls,
    h.created_at AS started_at,
    NULL::TIMESTAMPTZ AS ended_at,
    h.created_at
FROM ohvis_harness_traces h;

-- ── seed: 실패/저품질 trace 승격 기본 데이터셋 ──────────────────────────────

INSERT INTO llmops_datasets (slug, project, title, purpose, source_filter, metadata)
VALUES
    (
        'aads-failed-traces',
        'AADS',
        'AADS failed trace regression set',
        'Promote failed or low-quality AADS traces into an offline regression dataset.',
        '{"status": "error", "quality_score_lt": 0.4}'::JSONB,
        '{"origin": "migration_163", "evaluator": "rule"}'::JSONB
    ),
    (
        'ohvis-harness-regressions',
        'AADS',
        'OHVIS harness regression set',
        'Runner, goal loop, and deploy harness failures kept for rule evaluation.',
        '{"source": "ohvis_harness_traces"}'::JSONB,
        '{"origin": "migration_163", "evaluator": "rule"}'::JSONB
    )
ON CONFLICT (slug) DO NOTHING;
