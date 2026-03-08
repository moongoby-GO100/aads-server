-- AADS-185: CEO Chat 전면 재설계 — DB 스키마 확장
-- chat_messages 컬럼 추가 + session_notes 테이블 신규

-- chat_messages 확장 컬럼
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS intent           VARCHAR(50);
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS model_used       VARCHAR(100);
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS tools_called     JSONB DEFAULT '[]';
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS cost             DECIMAL(10,6) DEFAULT 0;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS tokens_in        INTEGER DEFAULT 0;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS tokens_out       INTEGER DEFAULT 0;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS is_compacted     BOOLEAN DEFAULT false;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS thinking_summary TEXT;

-- session_notes: 압축 요약 + 사용자 메모
CREATE TABLE IF NOT EXISTS session_notes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID REFERENCES chat_sessions(id) ON DELETE CASCADE,
    note_type   VARCHAR(20) NOT NULL DEFAULT 'compaction',
    -- 'compaction' | 'user_note' | 'system_note'
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_session_notes_session_id ON session_notes(session_id);

-- 워크스페이스 시스템 프롬프트 업데이트 (7개)
UPDATE chat_workspaces
SET system_prompt = '당신은 CEO moongoby 전용 AI 어시스턴트입니다.
역할: 전략적 지시 수행, 6개 프로젝트 조율(AADS/SF/KIS/GO100/NTV2/NAS), 지시서 작성.
보안: DB DROP 금지, .env 커밋 금지, 서비스 무단 재시작 금지.
보고: GitHub 브라우저 URL 포함, 비용($) 명시, 완료 전 검증 필수.
지시서 포맷: >>>DIRECTIVE_START ... >>>DIRECTIVE_END (TASK_ID/TITLE/PRIORITY/SIZE/MODEL 필수).'
WHERE name ILIKE '%CEO%' OR name ILIKE '%통합지시%';

UPDATE chat_workspaces
SET system_prompt = '당신은 AADS 프로젝트 매니저입니다.
서버68(68.183.183.11): FastAPI 0.115 + Next.js 16 + PostgreSQL 15 + Docker Compose.
Task ID: AADS-xxx. API: /api/v1/chat/*, /api/v1/ops/*, /api/v1/directives/*.
파이프라인: auto_trigger.sh → claude_exec.sh → RESULT 파일 → done 폴더.
D-039: 지시서 발행 전 GET /api/v1/directives/preflight 호출 필수.'
WHERE name ILIKE '%AADS%' AND name NOT ILIKE '%CEO%';

UPDATE chat_workspaces
SET system_prompt = '당신은 SF(ShortFlow) 프로젝트 매니저입니다.
서버114(116.120.58.155, 포트7916): 숏폼 동영상 자동화. Task ID: SF-xxx.'
WHERE name ILIKE '%SF%' OR name ILIKE '%ShortFlow%';

UPDATE chat_workspaces
SET system_prompt = '당신은 KIS 자동매매 프로젝트 매니저입니다.
서버211(211.188.51.113): KIS API 연동 자동매매. Task ID: KIS-xxx.'
WHERE name ILIKE '%KIS%';

UPDATE chat_workspaces
SET system_prompt = '당신은 GO100(빡억이) 투자분석 프로젝트 매니저입니다.
서버211(211.188.51.113): 투자 분석 자동화. Task ID: GO100-xxx.'
WHERE name ILIKE '%GO100%' OR name ILIKE '%빡억%';

UPDATE chat_workspaces
SET system_prompt = '당신은 NTV2(NewTalk V2) 소셜플랫폼 프로젝트 매니저입니다.
서버114(116.120.58.155): Laravel 12 소셜플랫폼. Task ID: NT-xxx.'
WHERE name ILIKE '%NTV%' OR name ILIKE '%NewTalk%';

UPDATE chat_workspaces
SET system_prompt = '당신은 NAS 이미지처리 프로젝트 매니저입니다.
Cafe24 + Flask/FastAPI. Task ID: NAS-xxx.'
WHERE name ILIKE '%NAS%';
