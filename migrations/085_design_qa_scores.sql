-- AADS-DESIGN-MOD-005
-- Static QA score persistence for Design Modification Studio.
-- Depends on migrations/084_design_modification_studio.sql.

CREATE TABLE IF NOT EXISTS design_qa_scores (
    request_id UUID PRIMARY KEY REFERENCES design_modification_requests(id) ON DELETE CASCADE,
    scoring_version TEXT NOT NULL DEFAULT 'static-v1',
    total_score INTEGER NOT NULL DEFAULT 0,
    request_match_score INTEGER NOT NULL DEFAULT 0,
    context_retention_score INTEGER NOT NULL DEFAULT 0,
    visual_completeness_score INTEGER NOT NULL DEFAULT 0,
    responsive_stability_score INTEGER NOT NULL DEFAULT 0,
    accessibility_score INTEGER NOT NULL DEFAULT 0,
    technical_stability_score INTEGER NOT NULL DEFAULT 0,
    score_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    token_compliance JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT design_qa_scores_total_score_check CHECK (
        total_score >= 0 AND total_score <= 100
    ),
    CONSTRAINT design_qa_scores_request_match_score_check CHECK (
        request_match_score >= 0 AND request_match_score <= 25
    ),
    CONSTRAINT design_qa_scores_context_retention_score_check CHECK (
        context_retention_score >= 0 AND context_retention_score <= 20
    ),
    CONSTRAINT design_qa_scores_visual_completeness_score_check CHECK (
        visual_completeness_score >= 0 AND visual_completeness_score <= 20
    ),
    CONSTRAINT design_qa_scores_responsive_stability_score_check CHECK (
        responsive_stability_score >= 0 AND responsive_stability_score <= 15
    ),
    CONSTRAINT design_qa_scores_accessibility_score_check CHECK (
        accessibility_score >= 0 AND accessibility_score <= 10
    ),
    CONSTRAINT design_qa_scores_technical_stability_score_check CHECK (
        technical_stability_score >= 0 AND technical_stability_score <= 10
    ),
    CONSTRAINT design_qa_scores_scoring_version_check CHECK (
        btrim(scoring_version) <> ''
    )
);

CREATE INDEX IF NOT EXISTS idx_design_qa_scores_total_updated
    ON design_qa_scores (total_score DESC, updated_at DESC);
