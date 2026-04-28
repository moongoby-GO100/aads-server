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
  ('project_pattern', 'kis_workdir', 'KIS 작업 디렉터리: /root/kis-autotrade-v4 (서버211). 소스 경로 project_config.py 참조.', 0.7, 'KIS'),
  ('project_pattern', 'kis_source_map', 'KIS 서비스별 소스 경로: [API서버] backend/app/main.py (uvicorn 8000, cwd=backend/). [라우터] backend/app/api/v1/ (32개: trading.py, live_trading.py, strategies.py, kis.py, admin.py, backtest.py, reports.py 등). [전략엔진] backend/app/services/strategy/ (m00_engine.py, order_executor.py, position_manager.py, signal_generator.py, execution_engine.py, stock_selector.py, risk_manager.py). [KIS API] backend/app/services/kis/ (client.py, improved_client.py, master_data.py, websocket_manager.py, sync_scheduler.py). [모델] backend/app/models/ (14개: user, strategy, position, trade, kis_config 등). [코어] backend/app/core/ (config.py, database.py, security.py, market_hours.py). [전략구현] backend/app/strategies/scalping/ (m00_first_3min_breakout.py, m01, m02, d01 등 20+). [통합트레이딩] backend/integrated_trading_system.py (systemd: integrated-trading.service). [데이터수집] backend/data_miner.py. [프론트엔드] frontend/ (trading.html, admin.html, strategies.html 등 23개 HTML + js/ 53개 JS).', 0.9, 'KIS'),
  ('project_pattern', 'kis_systemd', 'KIS systemd 서비스: kis-autotrade-api.service (uvicorn 8000), integrated-trading.service (integrated_trading_system.py), kis-autotrade-scalping.service. 실제 WorkingDirectory와 상태는 run_remote_command(project=''KIS'', command=''systemctl status ...'')로 확인 후 판단.', 0.8, 'KIS'),
  ('discovery', 'kis_server_independent', 'KIS(서버211)는 AADS(서버68)와 독립 — bridge.py 경유 실행', 0.6, 'KIS'),
  -- GO100
  ('project_pattern', 'go100_forced_exit', 'GO100 15:20 KST 강제 청산 시작, 15:25 전량 매도 — 오버나잇 금지', 0.8, 'GO100'),
  ('project_pattern', 'go100_claude_cost', 'GO100 Claude 호출은 5분봉 주요 시점에만 — 매 틱 호출 시 비용 폭증', 0.8, 'GO100'),
  ('project_pattern', 'go100_max_iterations', 'GO100 LangGraph 에이전트 max_iterations=50, 태스크당 $5 한도', 0.7, 'GO100'),
  ('project_pattern', 'go100_workdir', 'GO100 작업 디렉터리: /root/kis-autotrade-v4 (서버211). KIS와 코드베이스를 공유하되 project=''GO100''으로 도구를 호출한다.', 0.7, 'GO100'),
  ('project_pattern', 'go100_source_map', 'GO100 서비스별 소스 경로: KIS와 동일 구조. [API서버] backend/app/main.py. [라우터] backend/app/api/v1/. [전략] backend/app/services/strategy/. [KIS API] backend/app/services/kis/. [모델] backend/app/models/. [프론트엔드] frontend/. 서비스 상태는 health_check 또는 run_remote_command로 확인 후 판단한다.', 0.8, 'GO100'),
  ('discovery', 'go100_slippage', 'GO100 백테스트 vs 실거래 괴리: 슬리피지 0.05% + 수수료 0.015%', 0.6, 'GO100'),
  -- SF
  ('project_pattern', 'sf_pipeline', 'SF 파이프라인: Script(Claude) → TTS(ElevenLabs) → Render(FFmpeg) → Upload', 0.8, 'SF'),
  ('project_pattern', 'sf_ssh_port', 'SF 서버114 SSH 포트 7916 (비표준) — SSH_PORT 환경변수 필수', 0.8, 'SF'),
  ('project_pattern', 'sf_youtube_quota', 'SF YouTube API 일일 10K units — 하루 5영상 제한, 시간 분산 업로드', 0.7, 'SF'),
  ('project_pattern', 'sf_workdir', 'SF 작업 디렉터리: /data/shortflow (서버114:7916). 소스 경로 project_config.py 참조.', 0.7, 'SF'),
  ('project_pattern', 'sf_source_map', 'SF 서비스별 소스 경로 (4개 Docker 서비스): [worker — 영상 생성 파이프라인] worker/ (Docker shortflow-worker, 포트 8000). worker/main.py 진입점, worker/services/ (script_generator.py, tts_generator.py, tts_dual_engine.py, video_generator.py, video_editor.py, ffmpeg_composer.py, youtube_uploader.py, youtube_manager.py, image_generator.py, trend_collector.py, coupang_partners.py, coupang_browser.py, product_scorer.py, feedback_engine.py), worker/core/ (state_machine.py, retry_engine.py), worker/routes/, worker/config.py. [API서버] api/ (uvicorn 포트 8001). api/main.py, api/routers/ (platform_accounts.py, public_stats.py), api/routes/ (health.py, platform_accounts.py). [대시보드] dashboard/ (Docker shortflow-dashboard, Streamlit 포트 8501). dashboard/app.py, dashboard/api.py, dashboard/db.py, dashboard/pages/ (overview.py, analytics.py, script_qa.py, video_qa.py). [SaaS 대시보드] saas-dashboard (Docker shortflow-saas-dashboard, 포트 3001). [n8n] 워크플로우 자동화 (Docker shortflow-n8n, 포트 5678). [설정] config/ (channels, platforms.json, upload_metadata.json), channels/ (economy.json, health.json, history.json). [스크립트] scripts/ (sync_nas_to_server.sh, convert_batch.sh, daily_report.sh, alert_on_error.sh, log_rotate.sh, run_v4_pipeline.py 등).', 0.9, 'SF'),
  ('discovery', 'sf_elevenlabs_quota', 'ElevenLabs 무료 10K자/월 — 캐싱+요약으로 절약 필요', 0.6, 'SF'),
  -- NTV2
  ('project_pattern', 'ntv2_copyright', 'NTV2 뉴스 원문 직접 사용 금지 — Gemini 요약 → Claude 창작 재작성', 0.8, 'NTV2'),
  ('project_pattern', 'ntv2_pipeline', 'NTV2 파이프라인: 뉴스크롤 → 요약 → 스크립트 → TTS → 배포(팟캐스트+SNS)', 0.7, 'NTV2'),
  ('project_pattern', 'ntv2_workdir', 'NTV2 작업 디렉터리: /srv/newtalk-v2 (서버114:7916). 소스 경로 project_config.py 참조.', 0.7, 'NTV2'),
  ('project_pattern', 'ntv2_source_map', 'NTV2 서비스별 소스 경로 (6개 Docker 서비스): [Laravel 백엔드] src/ (Docker newtalk-v2-app, PHP-FPM 9000). src/가 Docker /var/www로 마운트. src/app/Http/Controllers/ (API 컨트롤러), src/app/Models/ (62개 Eloquent 모델: User, Product, Order, Content, Cart, Follow, Message, Settlement 등), src/app/Services/ (Cafe24ApiService, ContentPipelineService, ConversationService, DropshipService, FulfillmentService, MessageService, MessengerService), src/routes/api.php (API 라우트), src/config/ (app, auth, database, sanctum 등 11개), src/database/migrations/ (105개 마이그레이션). [Nginx] Docker newtalk-v2-nginx (포트 8080→80). docker/nginx/default.conf. [MySQL 8.0] Docker newtalk-v2-db (포트 3307→3306). [Redis 7] Docker newtalk-v2-redis (포트 6380→6379). [Reverb] Docker newtalk-v2-reverb (WebSocket, 포트 8081→8080). [Next.js 프론트엔드] frontend/ (Docker newtalk-v2-frontend, 포트 3000). frontend/src/app/ (페이지: admin, auth, md, outsource, purchaser, retail, wholesale), frontend/src/components/ (25+ 디렉터리: admin, cart, channel, content, dm, dropship, feed, fulfillment, layout, messenger, mypage 등). [스크립트] scripts/ (extract-db-schema.sh, report_to_aads.sh).', 0.9, 'NTV2'),
  ('discovery', 'ntv2_phase1_done', 'NTV2 Phase 1(환경구축) 완료, Phase 2(파이프라인 구현) 대기중', 0.6, 'NTV2'),
  -- NAS
  ('project_pattern', 'nas_mount_fstab', 'NAS 마운트 /etc/fstab에 nofail 옵션 필수 — 서버 재시작 시 마운트 해제 방지', 0.8, 'NAS'),
  ('project_pattern', 'nas_image_resize', 'NAS Claude Vision 이미지 20MB 제한 — 썸네일 2048x2048 리사이즈 필수', 0.7, 'NAS'),
  ('project_pattern', 'nas_backup_schedule', 'NAS 백업: 매일 02:00 KST rsync --update (증분) — full 백업은 타임아웃', 0.7, 'NAS'),
  ('project_pattern', 'nas_storage_alert', 'NAS 용량 80% 도달 시 텔레그램 알림 — 모니터링 필수', 0.7, 'NAS'),
  ('discovery', 'nas_workdir_root', 'NAS WORKDIR이 /root 전체 — auto_trigger.sh에 명시적 경로 지정 필요', 0.6, 'NAS')
ON CONFLICT (category, key, COALESCE(project, '')) DO NOTHING;
