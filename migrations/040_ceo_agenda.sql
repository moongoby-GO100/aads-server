-- AADS CEO 아젠다 관리 시스템
-- 마이그레이션: 040

CREATE TABLE IF NOT EXISTS ceo_agenda (
    id SERIAL PRIMARY KEY,
    project VARCHAR(20) NOT NULL,           -- AADS, KIS, GO100, SF, NTV2, NAS
    title VARCHAR(200) NOT NULL,
    summary TEXT NOT NULL,                  -- 핵심 논점 + 옵션 + 미결정 사항 (마크다운)
    status VARCHAR(20) NOT NULL DEFAULT '논의중',  -- 논의중, 보류, 결정, 진행중, 완료
    priority VARCHAR(5) DEFAULT 'P2',       -- P0, P1, P2, P3
    decision TEXT,                          -- CEO 결정 내용
    decision_at TIMESTAMPTZ,
    source_session_id VARCHAR(100),         -- 논의가 발생한 세션 ID
    tags TEXT[],                            -- 검색용 태그
    created_by VARCHAR(50) DEFAULT 'CEO',   -- CEO 또는 프로젝트명(CTO)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agenda_project ON ceo_agenda(project);
CREATE INDEX IF NOT EXISTS idx_agenda_status ON ceo_agenda(status);
CREATE INDEX IF NOT EXISTS idx_agenda_priority ON ceo_agenda(priority);
CREATE INDEX IF NOT EXISTS idx_agenda_created_at ON ceo_agenda(created_at DESC);
