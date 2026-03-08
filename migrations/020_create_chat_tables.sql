-- AADS-170: CEO Chat-First 시스템 DB 스키마
-- 6개 신규 테이블 + FTS/성능 인덱스

-- ─── chat_workspaces ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_workspaces (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       VARCHAR(100) NOT NULL,
    system_prompt TEXT,
    files      JSONB NOT NULL DEFAULT '[]',
    settings   JSONB NOT NULL DEFAULT '{}',
    color      VARCHAR(7) NOT NULL DEFAULT '#6366F1',
    icon       VARCHAR(10) NOT NULL DEFAULT '💬',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── chat_sessions ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  UUID NOT NULL REFERENCES chat_workspaces(id) ON DELETE CASCADE,
    title         VARCHAR(200),
    summary       TEXT,
    message_count INT NOT NULL DEFAULT 0,
    cost_total    DECIMAL(10,4) NOT NULL DEFAULT 0,
    pinned        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_workspace
    ON chat_sessions(workspace_id, updated_at DESC);

-- ─── chat_messages ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_messages (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id   UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role         VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content      TEXT NOT NULL,
    model_used   VARCHAR(50),
    intent       VARCHAR(30),
    cost         DECIMAL(10,6) NOT NULL DEFAULT 0,
    tokens_in    INT NOT NULL DEFAULT 0,
    tokens_out   INT NOT NULL DEFAULT 0,
    bookmarked   BOOLEAN NOT NULL DEFAULT FALSE,
    attachments  JSONB NOT NULL DEFAULT '[]',
    sources      JSONB NOT NULL DEFAULT '[]',
    artifact_id  UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON chat_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_fts
    ON chat_messages USING GIN (to_tsvector('simple', content));

-- ─── research_archive ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS research_archive (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic       VARCHAR(200) NOT NULL,
    query       TEXT NOT NULL,
    sources     JSONB NOT NULL,
    summary     TEXT NOT NULL,
    full_report TEXT,
    model_used  VARCHAR(50),
    cost        DECIMAL(10,4),
    session_id  UUID REFERENCES chat_sessions(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_research_fts
    ON research_archive USING GIN (to_tsvector('simple', topic || ' ' || summary));

-- ─── chat_artifacts ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_artifacts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    type       VARCHAR(20) NOT NULL CHECK (type IN ('report', 'code', 'chart', 'dashboard', 'table')),
    title      VARCHAR(200),
    content    TEXT NOT NULL,
    metadata   JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session
    ON chat_artifacts(session_id);

-- ─── chat_drive_files ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_drive_files (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES chat_workspaces(id),
    filename     VARCHAR(255) NOT NULL,
    file_path    VARCHAR(500) NOT NULL,
    file_type    VARCHAR(50),
    file_size    BIGINT NOT NULL DEFAULT 0,
    uploaded_by  VARCHAR(20) NOT NULL DEFAULT 'user',
    metadata     JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_drive_workspace
    ON chat_drive_files(workspace_id);

-- ─── 초기 워크스페이스 7개 시딩 ────────────────────────────────────────────
INSERT INTO chat_workspaces (name, system_prompt, color, icon) VALUES
    ('[CEO] 통합지시',      '당신은 CEO 전용 AI 어시스턴트입니다. CEO의 전략적 지시를 실행하고, 전체 프로젝트를 조율합니다.', '#6366F1', '👑'),
    ('[AADS] 프로젝트 매니저', '당신은 AADS 자율 AI 개발 시스템의 프로젝트 매니저입니다. 태스크 관리, 파이프라인 모니터링, 지시서 생성을 담당합니다.', '#8B5CF6', '🤖'),
    ('[SF] ShortFlow',     '당신은 ShortFlow 단편 동영상 자동화 시스템 전문가입니다. SF 관련 기술 문제, 배포, 모니터링을 지원합니다.', '#10B981', '🎬'),
    ('[KIS] 자동매매',      '당신은 KIS 자동매매 시스템 전문가입니다. 매매 전략, 백테스트, 시스템 안정성을 지원합니다.', '#F59E0B', '📈'),
    ('[GO100] 빡억이',      '당신은 GO100 투자 분석 시스템 전문가입니다. 종목 분석, 포트폴리오 관리, 수익률 최적화를 지원합니다.', '#EF4444', '💰'),
    ('[NTV2] NewTalk V2',  '당신은 NewTalk V2 소셜 플랫폼 전문가입니다. 기능 개발, 사용자 경험, 성능 최적화를 지원합니다.', '#06B6D4', '📱'),
    ('[NAS] Image',        '당신은 NAS 이미지 처리 시스템 전문가입니다. 이미지 생성, 처리, 저장 최적화를 지원합니다.', '#78716C', '🖼️')
ON CONFLICT DO NOTHING;
