-- OHVIS Loop System Phase 0 - DB Schema
-- 기획서: docs/AADS-LAYOUT-001_OHVIS-LOOP-SYSTEM.md §3

-- 1. ohvis_loops (루프 메타 정보)
CREATE TABLE IF NOT EXISTS ohvis_loops (
    id              SERIAL PRIMARY KEY,
    loop_type       VARCHAR(20) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',

    original_command TEXT NOT NULL,
    parsed_intent   JSONB NOT NULL DEFAULT '{}',

    interval_seconds INT,
    max_iterations  INT DEFAULT 50,
    max_cost_usd    DECIMAL(8,4) DEFAULT 0.50,
    execution_model_id VARCHAR(80),
    cost_override_by_ceo BOOLEAN DEFAULT FALSE,
    max_failures    INT DEFAULT 3,
    timeout_minutes INT DEFAULT 1440,

    success_condition JSONB,
    alert_condition   JSONB,

    current_iteration INT DEFAULT 0,
    consecutive_failures INT DEFAULT 0,
    total_cost_usd   DECIMAL(8,4) DEFAULT 0,
    last_result      JSONB,

    created_at      TIMESTAMP DEFAULT NOW(),
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    next_run_at     TIMESTAMP,

    created_by      VARCHAR(50) DEFAULT 'ceo',
    project         VARCHAR(20) DEFAULT 'AADS',
    session_id      VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_loops_status ON ohvis_loops(status);
CREATE INDEX IF NOT EXISTS idx_loops_next_run ON ohvis_loops(next_run_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_loops_project ON ohvis_loops(project);

-- 2. ohvis_loop_iterations (반복 실행 기록)
CREATE TABLE IF NOT EXISTS ohvis_loop_iterations (
    id              SERIAL PRIMARY KEY,
    loop_id         INT REFERENCES ohvis_loops(id) ON DELETE CASCADE,
    iteration_num   INT NOT NULL,

    status          VARCHAR(20) NOT NULL,
    result_summary  TEXT,
    result_data     JSONB,

    llm_calls       INT DEFAULT 0,
    cost_usd        DECIMAL(8,4) DEFAULT 0,
    duration_ms     INT,
    model_used      VARCHAR(80),

    alert_sent      BOOLEAN DEFAULT FALSE,
    alert_channel   VARCHAR(50),

    ohvis_task_id   INT,

    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_iterations_loop ON ohvis_loop_iterations(loop_id, iteration_num);

-- 3. ohvis_loop_definitions (프리셋 정의)
CREATE TABLE IF NOT EXISTS ohvis_loop_definitions (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) UNIQUE NOT NULL,
    description     TEXT,

    default_interval_seconds INT,
    default_max_iterations   INT,
    default_max_cost_usd     DECIMAL(8,4),
    default_alert_condition  JSONB,
    default_success_condition JSONB,

    task_template   JSONB NOT NULL DEFAULT '{}',

    is_active       BOOLEAN DEFAULT TRUE,
    project         VARCHAR(20) DEFAULT 'AADS',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 4. 시드 데이터: 기본 프리셋 5종 (AADS + 멀티프로젝트)
INSERT INTO ohvis_loop_definitions (name, description, project, default_interval_seconds, default_max_iterations, default_max_cost_usd, task_template, default_alert_condition)
VALUES
  ('server-health-monitor', 'AADS 서버 헬스 주기적 확인', 'AADS', 1800, 48, 0.30,
   '{"action":"health_check","targets":["aads-server","postgres","litellm"]}'::jsonb,
   '{"alert_on":["response_time > 3000ms","status != healthy"]}'::jsonb),

  ('disk-usage-monitor', '디스크 사용량 감시 (80% 경고)', 'AADS', 3600, 24, 0.15,
   '{"action":"run_command","command":"df -h / | awk ''NR==2{print $5}''"}'::jsonb,
   '{"alert_on":["usage_percent > 80"]}'::jsonb),

  ('deploy-until-success', '배포 성공할 때까지 재시도', 'AADS', NULL, 5, NULL,
   '{"action":"pipeline_runner","retry_strategy":"analyze_and_fix","success_condition":"health_check == 200"}'::jsonb,
   NULL),

  ('go100-market-monitor', '시장 데이터 주기 수집 및 매매 신호 감시', 'GO100', 300, 288, 0.50,
   '{"action":"run_command","command":"python3 scripts/check_market_signals.py"}'::jsonb,
   '{"alert_on":["signal_count > 0","price_change_pct > 3"]}'::jsonb),

  ('kis-position-monitor', '자동매매 포지션 실시간 감시', 'KIS', 60, 480, 0.50,
   '{"action":"run_command","command":"python3 scripts/check_positions.py"}'::jsonb,
   '{"alert_on":["unrealized_loss_pct > 5","margin_ratio < 150"]}'::jsonb)
ON CONFLICT (name) DO NOTHING;
