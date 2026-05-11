-- AADS-204 Open Design Hub Phase 0 schema draft.
-- This migration is intentionally not applied by the Phase 0 task.

CREATE TABLE IF NOT EXISTS design_projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    frontend_stack TEXT NOT NULL DEFAULT 'unknown',
    adapter_key TEXT NOT NULL DEFAULT 'legacy-css',
    repo_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS design_token_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key TEXT NOT NULL REFERENCES design_projects(project_key) ON DELETE CASCADE,
    version TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'project',
    tokens JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_key, version, mode)
);

CREATE TABLE IF NOT EXISTS design_audit_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key TEXT NOT NULL REFERENCES design_projects(project_key) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',
    score INTEGER,
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    scanned_files INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_design_audit_runs_project_created
    ON design_audit_runs (project_key, created_at DESC);
