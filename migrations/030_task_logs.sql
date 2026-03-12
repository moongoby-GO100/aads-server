-- 030: task_logs 테이블 — Pipeline B/C 실시간 작업 로그
CREATE TABLE IF NOT EXISTS task_logs (
    id          BIGSERIAL PRIMARY KEY,
    task_id     VARCHAR(100) NOT NULL,
    log_type    VARCHAR(20) NOT NULL DEFAULT 'info'
                CHECK (log_type IN ('info','command','output','error','phase_change')),
    content     TEXT NOT NULL,
    phase       VARCHAR(50),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_logs_created ON task_logs (created_at);

-- 7일 이상 된 로그 자동 삭제용 (GC에서 사용)
