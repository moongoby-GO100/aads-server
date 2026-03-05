-- AADS Memory Schema - init_memory_schema.sql
-- 5-Layer Memory Architecture (L2-L5)
-- 컨테이너 시작 시 /docker-entrypoint-initdb.d/ 를 통해 자동 실행
-- 전제: pgvector/pgvector:pg15 이미지 사용

-- pgvector 확장 활성화
CREATE EXTENSION IF NOT EXISTS vector;

-- ======================================================
-- L4: System Memory (HANDOVER 대체)
-- ======================================================
CREATE TABLE IF NOT EXISTS system_memory (
  id          SERIAL PRIMARY KEY,
  category    VARCHAR(50)  NOT NULL,
  key         VARCHAR(100) NOT NULL,
  value       JSONB        NOT NULL,
  version     VARCHAR(20),
  updated_at  TIMESTAMP    DEFAULT NOW(),
  updated_by  VARCHAR(50)  DEFAULT 'system',
  UNIQUE(category, key)
);
CREATE INDEX IF NOT EXISTS idx_system_memory_category ON system_memory(category);

-- ======================================================
-- L2: Project Memory (프로젝트 단위 장기기억)
-- ======================================================
CREATE TABLE IF NOT EXISTS project_memory (
  id          SERIAL PRIMARY KEY,
  project_id  VARCHAR(50)  NOT NULL,
  memory_type VARCHAR(30)  NOT NULL,
  content     JSONB        NOT NULL,
  embedding   vector(1536),
  created_at  TIMESTAMP    DEFAULT NOW(),
  updated_at  TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_project_memory_project   ON project_memory(project_id);
CREATE INDEX IF NOT EXISTS idx_project_memory_embedding ON project_memory
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ======================================================
-- L3: Experience Memory (프로젝트 간 경험/전략/교훈)
-- ======================================================
CREATE TABLE IF NOT EXISTS experience_memory (
  id              SERIAL PRIMARY KEY,
  experience_type VARCHAR(20)  NOT NULL CHECK (experience_type IN ('strategy', 'lesson')),
  domain          VARCHAR(50)  DEFAULT '',
  tags            TEXT[]       DEFAULT '{}',
  content         JSONB        NOT NULL,
  embedding       vector(1536),
  access_count    INTEGER      DEFAULT 0,
  last_accessed   TIMESTAMP    DEFAULT NOW(),
  rif_score       FLOAT        DEFAULT 1.0,
  created_at      TIMESTAMP    DEFAULT NOW(),
  updated_at      TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_experience_embedding ON experience_memory
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_experience_type ON experience_memory(experience_type);

-- ======================================================
-- L5: Procedural Memory (에이전트 절차기억)
-- ======================================================
CREATE TABLE IF NOT EXISTS procedural_memory (
  id             SERIAL PRIMARY KEY,
  agent_name     VARCHAR(30)  NOT NULL DEFAULT '',
  procedure_type VARCHAR(30)  NOT NULL DEFAULT '',
  content        JSONB        NOT NULL DEFAULT '{}',
  success_rate   FLOAT        DEFAULT 0.0,
  use_count      INTEGER      DEFAULT 0,
  updated_at     TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_procedural_memory_agent ON procedural_memory(agent_name);

-- ======================================================
-- go100_user_memory (T-038: 매니저 협업 API)
-- ======================================================
CREATE TABLE IF NOT EXISTS go100_user_memory (
  id          SERIAL PRIMARY KEY,
  user_id     INTEGER      NOT NULL DEFAULT 2,
  memory_type VARCHAR(100) NOT NULL,
  content     JSONB        NOT NULL DEFAULT '{}',
  importance  FLOAT        NOT NULL DEFAULT 5.0,
  expires_at  TIMESTAMP,
  created_at  TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_go100_user_memory_user_id     ON go100_user_memory(user_id);
CREATE INDEX IF NOT EXISTS idx_go100_user_memory_type        ON go100_user_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_go100_user_memory_importance  ON go100_user_memory(importance);
CREATE INDEX IF NOT EXISTS idx_go100_user_memory_created_at  ON go100_user_memory(created_at DESC);

-- ======================================================
-- T-038: 에러 자동기록·학습·자동복구
-- ======================================================
CREATE TABLE IF NOT EXISTS error_log (
    id SERIAL PRIMARY KEY,
    error_hash VARCHAR(64) NOT NULL,
    error_type VARCHAR(100) NOT NULL,
    source VARCHAR(100) NOT NULL,
    server VARCHAR(50) NOT NULL,
    message TEXT NOT NULL,
    stack_trace TEXT,
    context JSONB DEFAULT '{}',
    resolution TEXT,
    resolution_type VARCHAR(20) DEFAULT 'pending',
    resolved_at TIMESTAMP,
    auto_recoverable BOOLEAN DEFAULT FALSE,
    recovery_command TEXT,
    occurrence_count INTEGER DEFAULT 1,
    first_seen TIMESTAMP DEFAULT NOW(),
    last_seen TIMESTAMP DEFAULT NOW(),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_error_log_hash ON error_log(error_hash);
CREATE INDEX IF NOT EXISTS idx_error_log_type ON error_log(error_type);
CREATE INDEX IF NOT EXISTS idx_error_log_source ON error_log(source);
CREATE INDEX IF NOT EXISTS idx_error_log_last_seen ON error_log(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_error_log_pending ON error_log(resolution_type) WHERE resolution_type = 'pending';

-- 자동복구 이력
CREATE TABLE IF NOT EXISTS recovery_log (
    id SERIAL PRIMARY KEY,
    error_log_id INTEGER REFERENCES error_log(id),
    recovery_command TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    output TEXT,
    executed_at TIMESTAMP DEFAULT NOW()
);

-- ======================================================
-- T-039: CEO 승인 큐
-- ======================================================
CREATE TABLE IF NOT EXISTS approval_queue (
    id SERIAL PRIMARY KEY,
    error_log_id INTEGER REFERENCES error_log(id),
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    action_type VARCHAR(20) NOT NULL,          -- auto_command, claude_code, manual
    action_command TEXT,
    target_server VARCHAR(50) NOT NULL,        -- 68, 211, 114, NAS
    severity VARCHAR(20) DEFAULT 'medium',     -- critical, high, medium, low
    status VARCHAR(20) DEFAULT 'pending',      -- pending, approved, rejected, executed, failed
    telegram_message_id BIGINT,
    requested_at TIMESTAMP DEFAULT NOW(),
    responded_at TIMESTAMP,
    executed_at TIMESTAMP,
    execution_result TEXT,
    created_by VARCHAR(50) DEFAULT 'watchdog'
);

CREATE INDEX IF NOT EXISTS idx_approval_queue_status ON approval_queue(status);
CREATE INDEX IF NOT EXISTS idx_approval_queue_severity ON approval_queue(severity);
CREATE INDEX IF NOT EXISTS idx_approval_queue_telegram ON approval_queue(telegram_message_id);

-- T-039: 서버 감시 대상 등록 테이블
CREATE TABLE IF NOT EXISTS monitored_services (
    id SERIAL PRIMARY KEY,
    server VARCHAR(50) NOT NULL,
    service_name VARCHAR(100) NOT NULL,
    check_type VARCHAR(30) NOT NULL,           -- http_health, process, port, ssh_command
    check_target TEXT NOT NULL,
    check_interval INTEGER DEFAULT 30,
    timeout INTEGER DEFAULT 10,
    auto_recovery_command TEXT,
    enabled BOOLEAN DEFAULT TRUE,
    last_check TIMESTAMP,
    last_status VARCHAR(20) DEFAULT 'unknown', -- ok, error, timeout, unknown
    consecutive_failures INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_monitored_services_unique
    ON monitored_services(server, service_name);
