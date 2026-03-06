-- AADS-127: Ideation Subgraph — debate_logs 테이블
-- 적용: psql -U aads -d aads -f migrations/013_debate_logs.sql

CREATE TABLE IF NOT EXISTS debate_logs (
    id                  SERIAL PRIMARY KEY,
    project_id          UUID,
    round_number        INTEGER NOT NULL,
    strategist_message  JSONB NOT NULL,
    planner_message     JSONB NOT NULL,
    consensus_reached   BOOLEAN DEFAULT false,
    escalated           BOOLEAN DEFAULT false,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_debate_logs_project
    ON debate_logs(project_id);
