-- 목표 관리 스키마 (Phase 2: Control Loop)
CREATE TABLE IF NOT EXISTS goals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    description TEXT,
    project TEXT NOT NULL DEFAULT 'AADS',
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT DEFAULT 'P2',
    progress REAL DEFAULT 0.0,
    parent_goal_id UUID REFERENCES goals(id),
    created_by TEXT DEFAULT 'CEO',
    deadline TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS milestones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal_id UUID NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    sequence_order INTEGER NOT NULL DEFAULT 0,
    auto_advance BOOLEAN DEFAULT TRUE,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS goal_task_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    milestone_id UUID NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL DEFAULT 'pipeline_job',
    task_id TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_goals_project_status ON goals(project, status);
CREATE INDEX IF NOT EXISTS idx_milestones_goal_id ON milestones(goal_id);
CREATE INDEX IF NOT EXISTS idx_goal_task_links_milestone ON goal_task_links(milestone_id);
CREATE INDEX IF NOT EXISTS idx_goal_task_links_task ON goal_task_links(task_type, task_id);
