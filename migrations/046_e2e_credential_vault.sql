-- 046: E2E 테스트용 자격증명 저장소 (Credential Vault)
-- AADS-002: E2E 테스트 로그인 자격증명 암호화 저장·관리

CREATE TABLE IF NOT EXISTS e2e_credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service         VARCHAR(100) NOT NULL,          -- 'aads-dashboard', 'newtalk-admin', 'cafe24', 'kis-api' 등
    project         VARCHAR(20),                    -- AADS/KIS/GO100/SF/NTV2/NAS (NULL=공통)
    label           VARCHAR(100) NOT NULL DEFAULT '기본',  -- '관리자', 'CEO계정', '테스트계정'
    login_url       TEXT,                           -- 로그인 페이지 URL
    username_enc    TEXT NOT NULL,                   -- Fernet 암호화된 아이디
    password_enc    TEXT NOT NULL,                   -- Fernet 암호화된 비밀번호
    extra_fields    JSONB DEFAULT '{}',             -- 암호화된 추가 필드 (OTP secret, API key 등)
    login_steps     JSONB DEFAULT '[]',             -- Playwright 자동 로그인 스텝 정의
    is_active       BOOLEAN DEFAULT TRUE,
    last_used_at    TIMESTAMPTZ,
    last_verified   TIMESTAMPTZ,                    -- 마지막 로그인 성공 검증 시각
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 서비스+프로젝트+라벨 조합 유니크
CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_cred_service_project_label
    ON e2e_credentials (service, COALESCE(project, '_ALL_'), label);

-- 프로젝트별 조회 인덱스
CREATE INDEX IF NOT EXISTS idx_e2e_cred_project ON e2e_credentials (project);

COMMENT ON TABLE e2e_credentials IS 'E2E 테스트용 자격증명 저장소 — 모든 필드 Fernet 암호화';
COMMENT ON COLUMN e2e_credentials.login_steps IS 'Playwright 자동화 스텝 배열: [{"action":"fill","selector":"input#email","value":"{{username}}"},...]';
