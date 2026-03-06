-- AADS-132: 에러감지 시스템 고도화
-- recovery_logs: 복구 시도 이력, circuit_breaker_state: 서킷브레이커 상태

CREATE TABLE IF NOT EXISTS recovery_logs (
    id               SERIAL PRIMARY KEY,
    issue_type       TEXT NOT NULL,
    issue_data       JSONB NOT NULL,
    affected_task_id TEXT,
    affected_server  TEXT,
    tier             TEXT NOT NULL,
    action_taken     TEXT NOT NULL,
    result           TEXT NOT NULL,
    duration_seconds INTEGER,
    recovery_route   TEXT,
    error_message    TEXT,
    recovered_by     TEXT DEFAULT 'watchdog',
    created_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recovery_logs_type    ON recovery_logs(issue_type);
CREATE INDEX IF NOT EXISTS idx_recovery_logs_result  ON recovery_logs(result);
CREATE INDEX IF NOT EXISTS idx_recovery_logs_created ON recovery_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recovery_logs_server  ON recovery_logs(affected_server);

CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id              SERIAL PRIMARY KEY,
    server          TEXT NOT NULL UNIQUE,
    state           TEXT DEFAULT 'closed',
    failure_count   INTEGER DEFAULT 0,
    last_failure_at TIMESTAMP,
    cooldown_until  TIMESTAMP,
    opened_at       TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT NOW()
);

INSERT INTO circuit_breaker_state (server, state)
VALUES ('211', 'closed'), ('68', 'closed'), ('114', 'closed')
ON CONFLICT (server) DO NOTHING;
