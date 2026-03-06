-- AADS-125: Business Strategist — strategy_reports 테이블
-- 적용: psql -U aads -d aads -f migrations/011_strategy_reports.sql

CREATE TABLE IF NOT EXISTS strategy_reports (
    id                SERIAL PRIMARY KEY,
    project_id        UUID,  -- projects 테이블 미존재 시 FK 없이 운영
    direction         TEXT NOT NULL,
    strategy_report   JSONB NOT NULL,
    candidates        JSONB NOT NULL,
    recommendation    TEXT,
    total_sources     INTEGER DEFAULT 0,
    cost_usd          NUMERIC(10,4) DEFAULT 0,
    model_used        TEXT,
    created_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategy_reports_project
    ON strategy_reports(project_id);

CREATE INDEX IF NOT EXISTS idx_strategy_reports_created
    ON strategy_reports(created_at DESC);
