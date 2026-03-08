-- AADS-186C: 알림 이력 테이블
-- Telegram 알림 발송 이력 + AlertManager 규칙 평가 결과 저장

CREATE TABLE IF NOT EXISTS alert_history (
    id               SERIAL PRIMARY KEY,
    severity         VARCHAR(20) NOT NULL,            -- 'CRITICAL', 'WARNING', 'INFO'
    category         VARCHAR(30) NOT NULL,            -- 'server_down', 'health_fail', 'cost_exceed', 'disk_full', 'task_stall', 'ssh_timeout', 'memory_high', 'pat_expiry'
    title            TEXT NOT NULL,
    message          TEXT NOT NULL,
    server           VARCHAR(20),                     -- '68', '211', '114' 또는 NULL (전체)
    project          VARCHAR(50),                     -- 'AADS', 'GO100', 'KIS', 'SF', 'NTV2', 'NAS' 또는 NULL
    acknowledged     BOOLEAN DEFAULT FALSE,
    acknowledged_at  TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- 인덱스: 미확인 알림 조회 최적화
CREATE INDEX IF NOT EXISTS idx_alert_history_acknowledged
    ON alert_history (acknowledged, created_at DESC);

-- 인덱스: 중복 방지 쿼리 최적화 (동일 카테고리+서버 1시간 내)
CREATE INDEX IF NOT EXISTS idx_alert_history_dedup
    ON alert_history (category, server, created_at DESC);

-- 인덱스: 심각도별 조회
CREATE INDEX IF NOT EXISTS idx_alert_history_severity
    ON alert_history (severity, created_at DESC);
