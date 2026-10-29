-- PENDING: AADS-DESIGN-AGENT-D16-INTAKE-SCOPE-GATE-20261029
CREATE TABLE IF NOT EXISTS design_intake_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    project_key TEXT,
    screen_ref TEXT,
    round SMALLINT NOT NULL CHECK (round BETWEEN 1 AND 3),
    brief JSONB NOT NULL DEFAULT '{}'::jsonb,
    brief_confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id)
);
CREATE TABLE IF NOT EXISTS design_intake_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    area TEXT,
    question TEXT,
    options JSONB NOT NULL,
    recommended TEXT,
    answer TEXT,
    answer_source TEXT CHECK (answer_source IN
        ('ceo_choice','ceo_input','recommended_accepted','estimate_confirmed')),
    confirmed_at TIMESTAMPTZ,
    FOREIGN KEY (session_id, tenant_id) REFERENCES design_intake_sessions(id, tenant_id)
);
