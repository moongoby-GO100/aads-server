-- AADS-DESIGN-MOD-001
-- Design Modification Studio foundational schema.
-- Depends on migrations/082_open_design_hub.sql for design_projects.

CREATE TABLE IF NOT EXISTS design_screens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key TEXT NOT NULL REFERENCES design_projects(project_key) ON DELETE CASCADE,
    route TEXT NOT NULL,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    primary_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    component_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_design_screens_project_route UNIQUE (project_key, route)
);

CREATE TABLE IF NOT EXISTS design_modification_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key TEXT NOT NULL REFERENCES design_projects(project_key) ON DELETE CASCADE,
    screen_id UUID NULL REFERENCES design_screens(id) ON DELETE SET NULL,
    user_prompt TEXT NOT NULL,
    normalized_card JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_type TEXT NOT NULL DEFAULT 'other',
    allowed_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    forbidden_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    acceptance_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT design_modification_requests_type_check CHECK (
        request_type IN (
            'spacing',
            'spacing_density',
            'visual_hierarchy',
            'color',
            'color_brand',
            'typography',
            'component',
            'component_consistency',
            'responsive',
            'interaction',
            'content_clarity',
            'workflow_layout',
            'flow',
            'other'
        )
    ),
    CONSTRAINT design_modification_requests_status_check CHECK (
        status IN ('draft', 'ready', 'running', 'review', 'approved', 'rejected')
    )
);

CREATE TABLE IF NOT EXISTS design_context_packs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES design_modification_requests(id) ON DELETE CASCADE,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    missing_context JSONB NOT NULL DEFAULT '[]'::jsonb,
    prompt_chars INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT design_context_packs_prompt_chars_check CHECK (prompt_chars >= 0)
);

CREATE TABLE IF NOT EXISTS design_visual_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES design_modification_requests(id) ON DELETE CASCADE,
    phase TEXT NOT NULL DEFAULT 'before',
    viewport TEXT NOT NULL,
    image_url TEXT NOT NULL,
    dom_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT design_visual_snapshots_phase_check CHECK (
        phase IN ('before', 'after', 'regression')
    )
);

CREATE TABLE IF NOT EXISTS design_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key TEXT NOT NULL REFERENCES design_projects(project_key) ON DELETE CASCADE,
    screen_id UUID NULL REFERENCES design_screens(id) ON DELETE SET NULL,
    subject TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT,
    applies_to TEXT NOT NULL DEFAULT 'project',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    supersedes_id UUID NULL REFERENCES design_decisions(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT design_decisions_applies_to_check CHECK (
        applies_to IN ('global', 'project', 'screen', 'component')
    ),
    CONSTRAINT design_decisions_confidence_check CHECK (
        confidence >= 0 AND confidence <= 1
    )
);

CREATE INDEX IF NOT EXISTS idx_design_screens_project_route
    ON design_screens (project_key, route, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_design_mod_requests_project_status_created
    ON design_modification_requests (project_key, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_design_mod_requests_screen_created
    ON design_modification_requests (screen_id, created_at DESC)
    WHERE screen_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_design_context_packs_request_created
    ON design_context_packs (request_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_design_visual_snapshots_request_phase_captured
    ON design_visual_snapshots (request_id, phase, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_design_decisions_project_screen_created
    ON design_decisions (project_key, screen_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_design_decisions_supersedes
    ON design_decisions (supersedes_id)
    WHERE supersedes_id IS NOT NULL;
