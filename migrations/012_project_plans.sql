-- AADS-126: Planner Agent — project_plans 테이블
-- 적용: psql -U aads -d aads -f migrations/012_project_plans.sql
-- 주의: projects 테이블 미존재 시 FK 없이 운영 (strategy_reports 패턴 동일)

CREATE TABLE IF NOT EXISTS project_plans (
    id                      SERIAL PRIMARY KEY,
    project_id              UUID,
    strategy_report_id      INTEGER REFERENCES strategy_reports(id),
    selected_candidate_id   TEXT NOT NULL,
    prd                     JSONB NOT NULL,
    architecture            JSONB NOT NULL,
    phase_plan              JSONB NOT NULL,
    rejected_alternatives   JSONB DEFAULT '[]',
    debate_rounds           INTEGER DEFAULT 0,
    consensus_reached       BOOLEAN DEFAULT false,
    debate_log              JSONB DEFAULT '[]',
    cost_usd                NUMERIC(10,4) DEFAULT 0,
    status                  TEXT DEFAULT 'draft',
    created_at              TIMESTAMP DEFAULT NOW(),
    approved_at             TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_project_plans_project
    ON project_plans(project_id);

CREATE INDEX IF NOT EXISTS idx_project_plans_status
    ON project_plans(status);
