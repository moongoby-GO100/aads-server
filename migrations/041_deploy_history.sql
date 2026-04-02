-- 041: 배포 이력 DB 기록 테이블
-- P2: 문서 대신 DB에 배포 이력 자동 기록
-- 2026-04-02

CREATE TABLE IF NOT EXISTS deploy_history (
    id          SERIAL PRIMARY KEY,
    deploy_type VARCHAR(20) NOT NULL DEFAULT 'code_only',  -- code_only | blue_green | rollback
    project     VARCHAR(20) NOT NULL DEFAULT 'AADS',
    trigger_by  VARCHAR(50) NOT NULL DEFAULT 'script',     -- script | pipeline_runner | manual | chat_direct
    git_commit  VARCHAR(40),
    git_message TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'started',    -- started | success | failed | rolled_back
    duration_s  INTEGER,
    error_msg   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_deploy_history_created ON deploy_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deploy_history_project ON deploy_history(project);
CREATE INDEX IF NOT EXISTS idx_deploy_history_status  ON deploy_history(status);

COMMENT ON TABLE deploy_history IS '배포 이력 자동 기록 (P2: DEV-FLOW v1.1)';
