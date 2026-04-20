CREATE TABLE IF NOT EXISTS braming_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'completed', 'archived')),
    config JSONB DEFAULT '{}',
    summary TEXT,
    total_cost FLOAT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS braming_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES braming_sessions(id) ON DELETE CASCADE,
    parent_id UUID REFERENCES braming_nodes(id) ON DELETE SET NULL,
    node_type TEXT NOT NULL CHECK (
        node_type IN ('topic', 'perspective', 'idea', 'counter', 'expansion', 'synthesis', 'ceo_pick')
    ),
    label TEXT NOT NULL,
    content TEXT,
    agent_role TEXT,
    position_x FLOAT DEFAULT 0,
    position_y FLOAT DEFAULT 0,
    metadata JSONB DEFAULT '{}',
    cost FLOAT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_braming_nodes_session ON braming_nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_braming_nodes_parent ON braming_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_braming_sessions_status ON braming_sessions(status);
