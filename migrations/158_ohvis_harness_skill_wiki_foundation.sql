-- 158: OHVIS Harness / Skill Find / LLM Wiki foundation.
-- Date: 2026-09-07
--
-- This migration is additive only. Runtime code gracefully falls back to
-- memory_facts and built-in skill specs until these tables are applied.

CREATE TABLE IF NOT EXISTS ops_skill_library (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    projects TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    intents TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    risk_tier TEXT NOT NULL DEFAULT 'read',
    allowed_tools TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    validation JSONB NOT NULL DEFAULT '[]'::JSONB,
    source_path TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ops_skill_library_scope
    ON ops_skill_library USING GIN (projects);

CREATE INDEX IF NOT EXISTS idx_ops_skill_library_intents
    ON ops_skill_library USING GIN (intents);

CREATE TABLE IF NOT EXISTS ops_skill_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id UUID NOT NULL REFERENCES ops_skill_library(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (skill_id, version)
);

CREATE TABLE IF NOT EXISTS ops_skill_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id UUID REFERENCES ops_skill_library(id) ON DELETE SET NULL,
    skill_slug TEXT NOT NULL,
    project TEXT,
    session_id UUID,
    ohvis_task_id UUID REFERENCES ohvis_tasks(id) ON DELETE SET NULL,
    graph_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    input JSONB NOT NULL DEFAULT '{}'::JSONB,
    output JSONB NOT NULL DEFAULT '{}'::JSONB,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ops_skill_runs_project_status
    ON ops_skill_runs (project, status, started_at DESC);

CREATE TABLE IF NOT EXISTS ohvis_wiki_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT,
    source_type TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT,
    fetched_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ohvis_wiki_sources_uri_hash
    ON ohvis_wiki_sources (source_uri, COALESCE(content_sha256, ''));

CREATE TABLE IF NOT EXISTS ohvis_wiki_pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    source_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    stale_after TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project, slug)
);

CREATE INDEX IF NOT EXISTS idx_ohvis_wiki_pages_project
    ON ohvis_wiki_pages (project, updated_at DESC);

CREATE TABLE IF NOT EXISTS ohvis_wiki_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_page_id UUID NOT NULL REFERENCES ohvis_wiki_pages(id) ON DELETE CASCADE,
    to_page_id UUID REFERENCES ohvis_wiki_pages(id) ON DELETE CASCADE,
    to_uri TEXT,
    relation TEXT NOT NULL DEFAULT 'related_to',
    evidence TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (to_page_id IS NOT NULL OR to_uri IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS ohvis_wiki_error_book (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT,
    error_key TEXT NOT NULL,
    symptom TEXT NOT NULL,
    root_cause TEXT NOT NULL DEFAULT '',
    prevention TEXT NOT NULL DEFAULT '',
    source_task_id UUID REFERENCES ohvis_tasks(id) ON DELETE SET NULL,
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project, error_key)
);

CREATE TABLE IF NOT EXISTS ohvis_harness_traces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_run_id TEXT NOT NULL,
    project TEXT,
    session_id UUID,
    ohvis_task_id UUID REFERENCES ohvis_tasks(id) ON DELETE SET NULL,
    provider TEXT NOT NULL DEFAULT 'internal',
    trace_id TEXT,
    span_id TEXT,
    run_type TEXT NOT NULL DEFAULT 'chain',
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    tool_calls JSONB NOT NULL DEFAULT '[]'::JSONB,
    latency_ms INTEGER,
    cost_usd NUMERIC(12, 6),
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ohvis_harness_traces_graph_run
    ON ohvis_harness_traces (graph_run_id, created_at DESC);

INSERT INTO ops_skill_library
    (slug, title, description, projects, intents, risk_tier, allowed_tools, validation, source_path, metadata)
VALUES
    (
        'aads-bluegreen-release',
        'AADS blue-green release',
        'AADS backend/dashboard immutable SHA blue-green release runbook.',
        ARRAY['AADS'], ARRAY['deploy','release','ops'], 'deploy',
        ARRAY['git','docker','deploy.sh','curl','query_database'],
        '["clean release SHA","candidate health","same digest standby","5m P0/P1 monitor"]'::jsonb,
        'builtin:aads-bluegreen-release',
        '{"harness":"release"}'::jsonb
    ),
    (
        'runner-recovery',
        'Pipeline Runner recovery',
        'Recover stale, failed, or awaiting-approval pipeline runner jobs with evidence-first reporting.',
        ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], ARRAY['task_query','pipeline','ops','recovery'], 'write',
        ARRAY['pipeline_runner_status','read_task_logs','terminate_task'],
        '["status requery","log evidence","stale/error separation"]'::jsonb,
        'builtin:runner-recovery',
        '{"harness":"ops"}'::jsonb
    ),
    (
        'authenticated-site-collector',
        'Authenticated site collector',
        'Login-required site collection with CAPTCHA/OTP bypass block and same work_key resume.',
        ARRAY['AADS','CEO'], ARRAY['browser_collection','pc_agent','auth','marketing'], 'auth',
        ARRAY['pc_agent','browser_bridge','credential_vault','browser_tasks'],
        '["captcha/otp bypass blocked","same work_key resume","dry-run before collection"]'::jsonb,
        'builtin:authenticated-site-collector',
        '{"harness":"browser_collection"}'::jsonb
    ),
    (
        'store-assistant-channel-collector',
        'Store assistant channel collector',
        'Store assistant and marketing channel collection profile for Baemin, SmartPlace, CoupangEats, and similar channels.',
        ARRAY['AADS','CEO'], ARRAY['browser_collection','store_assistant','marketing'], 'auth',
        ARRAY['pc_agent','browser_bridge','browser_recipes'],
        '["site profile","account policy","manual challenge resume"]'::jsonb,
        'builtin:store-assistant-channel-collector',
        '{"harness":"browser_collection"}'::jsonb
    ),
    (
        'go100-market-open-check',
        'GO100 market open check',
        'GO100 market-open entry, data quality, and signal audit with financial risk gate.',
        ARRAY['GO100'], ARRAY['ops','finance','audit'], 'financial',
        ARRAY['query_project_database','run_remote_command','read_remote_file'],
        '["stock names included","read-only first","order gate respected"]'::jsonb,
        'builtin:go100-market-open-check',
        '{"harness":"finance_ops"}'::jsonb
    ),
    (
        'kis-broker-health',
        'KIS broker health and risk gate',
        'KIS broker account/session/order health audit with financial action approval gate.',
        ARRAY['KIS'], ARRAY['ops','finance','health'], 'financial',
        ARRAY['query_project_database','run_remote_command'],
        '["broker session checked","order risk gate","read-only report"]'::jsonb,
        'builtin:kis-broker-health',
        '{"harness":"finance_ops"}'::jsonb
    ),
    (
        'ntv2-merchant-contract',
        'NTV2 merchant contract workflow',
        'NewTalk V2 merchant contract, onboarding document, and template operation skill.',
        ARRAY['NTV2'], ARRAY['contract','merchant','docs'], 'write',
        ARRAY['read_remote_file','run_remote_command','export_data'],
        '["template source","tenant scope","document preview"]'::jsonb,
        'builtin:ntv2-merchant-contract',
        '{"harness":"contract_ops"}'::jsonb
    ),
    (
        'sf-video-pipeline-health',
        'ShortFlow video pipeline health',
        'ShortFlow video generation queue and worker health audit.',
        ARRAY['SF'], ARRAY['ops','video','health'], 'read',
        ARRAY['run_remote_command','list_remote_dir','read_remote_file'],
        '["queue count","worker health","latest error sample"]'::jsonb,
        'builtin:sf-video-pipeline-health',
        '{"harness":"media_ops"}'::jsonb
    ),
    (
        'nas-image-job-health',
        'NAS image job health',
        'NAS image-processing queue, storage, and failure health audit.',
        ARRAY['NAS'], ARRAY['ops','image','health'], 'read',
        ARRAY['run_remote_command','list_remote_dir'],
        '["storage capacity","job queue","recent failures"]'::jsonb,
        'builtin:nas-image-job-health',
        '{"harness":"media_ops"}'::jsonb
    )
ON CONFLICT (slug) DO UPDATE
SET title = EXCLUDED.title,
    description = EXCLUDED.description,
    projects = EXCLUDED.projects,
    intents = EXCLUDED.intents,
    risk_tier = EXCLUDED.risk_tier,
    allowed_tools = EXCLUDED.allowed_tools,
    validation = EXCLUDED.validation,
    source_path = EXCLUDED.source_path,
    metadata = EXCLUDED.metadata,
    enabled = TRUE,
    updated_at = NOW();
