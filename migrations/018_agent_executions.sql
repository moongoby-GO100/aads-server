-- AADS-164: CEO Chat Agent Individual Call System
-- agent_executions: CEO Chat에서 에이전트 개별 호출 이력 저장
CREATE TABLE IF NOT EXISTS agent_executions (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,          -- qa, judge, architect, developer, researcher, design
    intent TEXT NOT NULL,              -- classify_intent 결과
    input_summary TEXT,                -- CEO 메시지 요약
    output_summary TEXT,               -- 에이전트 결과 요약
    status TEXT DEFAULT 'running',     -- running, success, error
    cost_usd FLOAT DEFAULT 0,
    duration_ms INT DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_executions_session ON agent_executions(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_executions_agent_type ON agent_executions(agent_type);
