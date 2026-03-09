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

-- ======================================================
-- AADS-186E-2: 4계층 메모리 — session_notes (Working Memory)
-- ======================================================
-- session_notes는 chat_sessions FK 없이 독립 (도구 save_note에서 "note_xxx" 형태 session_id 사용)
CREATE TABLE IF NOT EXISTS session_notes (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(100),
    summary TEXT NOT NULL,
    key_decisions TEXT[] DEFAULT '{}',
    action_items TEXT[] DEFAULT '{}',
    unresolved_issues TEXT[] DEFAULT '{}',
    projects_discussed VARCHAR(50)[] DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_session_notes_session_id ON session_notes (session_id);
CREATE INDEX IF NOT EXISTS idx_session_notes_created_at ON session_notes (created_at DESC);

-- ======================================================
-- AADS-186E-2: ai_meta_memory (Meta Memory Layer 4)
-- CEO 선호도, 프로젝트 패턴, 알려진 이슈, 결정 이력
-- ======================================================
CREATE TABLE IF NOT EXISTS ai_meta_memory (
    id SERIAL PRIMARY KEY,
    category VARCHAR(30) NOT NULL,
    key VARCHAR(100) NOT NULL UNIQUE,
    value JSONB NOT NULL,
    confidence FLOAT DEFAULT 1.0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_meta_memory_category ON ai_meta_memory (category);
CREATE INDEX IF NOT EXISTS idx_ai_meta_memory_updated_at ON ai_meta_memory (updated_at DESC);

-- ======================================================
-- AADS-186E-3: ai_observations (AI 자동 관찰)
-- Haiku가 대화에서 CEO 선호/패턴/이슈 자동 추출·누적
-- ======================================================
CREATE TABLE IF NOT EXISTS ai_observations (
    id SERIAL PRIMARY KEY,
    category VARCHAR(30) NOT NULL,
    key VARCHAR(100) NOT NULL,
    value TEXT NOT NULL,
    confidence FLOAT DEFAULT 0.5,
    source_session_id INTEGER,
    last_confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    project VARCHAR(20) DEFAULT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_observations_cat_key_proj ON ai_observations (category, key, COALESCE(project, ''));
CREATE INDEX IF NOT EXISTS idx_ai_observations_category ON ai_observations (category);
CREATE INDEX IF NOT EXISTS idx_ai_observations_confidence ON ai_observations (confidence DESC);
CREATE INDEX IF NOT EXISTS idx_ai_observations_project ON ai_observations (project);

-- 시드 데이터: CEO 선호 + 운영 패턴 (ON CONFLICT 무시)
INSERT INTO ai_observations (category, key, value, confidence, project) VALUES
  ('ceo_preference', 'language_korean', '한국어 우선, CEO 보고는 한국어로', 0.9, NULL),
  ('ceo_preference', 'cost_efficiency', '최고 성능을 유지하면서 비용 효율적이어야 함', 0.8, NULL),
  ('ceo_preference', 'optimize_over_replace', '모델 교체보다 같은 모델 내 최적화 우선', 0.8, NULL),
  ('ceo_preference', 'report_format', '보고서는 정형화된 포맷으로, 캐주얼 대화는 자연스럽게', 0.7, NULL),
  ('ceo_preference', 'concise_reports', '간결한 보고 선호 — 불필요한 설명 제거, 핵심만 전달', 0.8, NULL),
  ('decision', 'main_model_claude', 'DeepSeek는 fallback 전용, 메인은 Claude 유지', 0.9, NULL),
  ('decision', 'default_model_sonnet', 'Sonnet 4.6을 기본 모델로, Opus는 심층 분석에만', 0.8, NULL),
  ('decision', 'prompt_caching_3block', 'Prompt Caching 3-Block 구조 적용 (1h/5min/no-cache)', 0.7, NULL),
  ('project_pattern', 'aads_chat_timeout', '채팅 SSE 타임아웃 수정 후 heartbeat 도입 — 8초 간격 keep-alive', 0.7, 'AADS'),
  ('project_pattern', 'status_check_order', 'CEO가 진행 확인 요청 시 task_history → service_status 순서로 조회', 0.7, NULL),
  ('project_pattern', 'code_analysis_order', '코드 분석 시 code_explorer 먼저 실행 후 결과 기반으로 보고', 0.6, NULL),
  ('project_pattern', 'health_check_order', '서버 상태 확인 시 health_check → dashboard_query 순서', 0.6, NULL),
  ('project_pattern', 'deep_research_cost', 'Deep Research는 비용이 크므로 꼭 필요할 때만 사용', 0.7, NULL),
  -- KIS
  ('project_pattern', 'kis_token_refresh', 'KIS Access Token 23시간마다 갱신 필수 — 만료 시 주문 실패', 0.8, 'KIS'),
  ('project_pattern', 'kis_market_hours', 'KIS 매매 가능 시간: 09:00~15:30 KST — 장 마감 후 주문 거부', 0.8, 'KIS'),
  ('project_pattern', 'kis_paper_vs_live', 'KIS 모의투자(paper) vs 실거래(live) URL 엔드포인트 구분 필수', 0.7, 'KIS'),
  ('project_pattern', 'kis_workdir', 'KIS 작업 디렉터리: /root/kis-autotrade-v4 (서버211)', 0.7, 'KIS'),
  ('discovery', 'kis_server_independent', 'KIS(서버211)는 AADS(서버68)와 독립 — bridge.py 경유 실행', 0.6, 'KIS'),
  -- GO100
  ('project_pattern', 'go100_forced_exit', 'GO100 15:20 KST 강제 청산 시작, 15:25 전량 매도 — 오버나잇 금지', 0.8, 'GO100'),
  ('project_pattern', 'go100_claude_cost', 'GO100 Claude 호출은 5분봉 주요 시점에만 — 매 틱 호출 시 비용 폭증', 0.8, 'GO100'),
  ('project_pattern', 'go100_max_iterations', 'GO100 LangGraph 에이전트 max_iterations=50, 태스크당 $5 한도', 0.7, 'GO100'),
  ('project_pattern', 'go100_workdir', 'GO100 작업 디렉터리: /root/kis-autotrade-v4/go100/ (서버211)', 0.7, 'GO100'),
  ('discovery', 'go100_slippage', 'GO100 백테스트 vs 실거래 괴리: 슬리피지 0.05% + 수수료 0.015%', 0.6, 'GO100'),
  -- SF
  ('project_pattern', 'sf_pipeline', 'SF 파이프라인: Script(Claude) → TTS(ElevenLabs) → Render(FFmpeg) → Upload', 0.8, 'SF'),
  ('project_pattern', 'sf_ssh_port', 'SF 서버114 SSH 포트 7916 (비표준) — SSH_PORT 환경변수 필수', 0.8, 'SF'),
  ('project_pattern', 'sf_youtube_quota', 'SF YouTube API 일일 10K units — 하루 5영상 제한, 시간 분산 업로드', 0.7, 'SF'),
  ('project_pattern', 'sf_workdir', 'SF 작업 디렉터리: /data/shortflow (서버114:7916)', 0.7, 'SF'),
  ('discovery', 'sf_elevenlabs_quota', 'ElevenLabs 무료 10K자/월 — 캐싱+요약으로 절약 필요', 0.6, 'SF'),
  -- NTV2
  ('project_pattern', 'ntv2_copyright', 'NTV2 뉴스 원문 직접 사용 금지 — Gemini 요약 → Claude 창작 재작성', 0.8, 'NTV2'),
  ('project_pattern', 'ntv2_pipeline', 'NTV2 파이프라인: 뉴스크롤 → 요약 → 스크립트 → TTS → 배포(팟캐스트+SNS)', 0.7, 'NTV2'),
  ('project_pattern', 'ntv2_workdir', 'NTV2 작업 디렉터리: /srv/newtalk-v2 (서버114:7916)', 0.7, 'NTV2'),
  ('discovery', 'ntv2_phase1_done', 'NTV2 Phase 1(환경구축) 완료, Phase 2(파이프라인 구현) 대기중', 0.6, 'NTV2'),
  -- NAS
  ('project_pattern', 'nas_mount_fstab', 'NAS 마운트 /etc/fstab에 nofail 옵션 필수 — 서버 재시작 시 마운트 해제 방지', 0.8, 'NAS'),
  ('project_pattern', 'nas_image_resize', 'NAS Claude Vision 이미지 20MB 제한 — 썸네일 2048x2048 리사이즈 필수', 0.7, 'NAS'),
  ('project_pattern', 'nas_backup_schedule', 'NAS 백업: 매일 02:00 KST rsync --update (증분) — full 백업은 타임아웃', 0.7, 'NAS'),
  ('project_pattern', 'nas_storage_alert', 'NAS 용량 80% 도달 시 텔레그램 알림 — 모니터링 필수', 0.7, 'NAS'),
  ('discovery', 'nas_workdir_root', 'NAS WORKDIR이 /root 전체 — auto_trigger.sh에 명시적 경로 지정 필요', 0.6, 'NAS')
ON CONFLICT (category, key, COALESCE(project, '')) DO NOTHING;
