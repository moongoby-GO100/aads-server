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
