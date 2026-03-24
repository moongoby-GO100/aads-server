-- 038: KakaoBot AI SaaS — Phase 1 핵심 테이블
-- 연락처, 기념일, 템플릿, 예약발송

-- 1. 연락처
CREATE TABLE IF NOT EXISTS kakaobot_contacts (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL DEFAULT 'default',
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    group_name VARCHAR(100) DEFAULT '',
    relationship VARCHAR(50) DEFAULT '',
    memo TEXT DEFAULT '',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kakaobot_contacts_user ON kakaobot_contacts(user_id);
CREATE INDEX IF NOT EXISTS idx_kakaobot_contacts_phone ON kakaobot_contacts(user_id, phone);

-- 2. 기념일
CREATE TABLE IF NOT EXISTS kakaobot_anniversaries (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES kakaobot_contacts(id) ON DELETE CASCADE,
    user_id VARCHAR(100) NOT NULL DEFAULT 'default',
    title VARCHAR(200) NOT NULL,
    date DATE NOT NULL,
    is_lunar BOOLEAN DEFAULT FALSE,
    recurrence VARCHAR(20) DEFAULT 'yearly',
    remind_days_before INTEGER DEFAULT 1,
    auto_send BOOLEAN DEFAULT FALSE,
    template_id INTEGER DEFAULT NULL,
    custom_message TEXT DEFAULT '',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kakaobot_anniv_user ON kakaobot_anniversaries(user_id);
CREATE INDEX IF NOT EXISTS idx_kakaobot_anniv_date ON kakaobot_anniversaries(date);
CREATE INDEX IF NOT EXISTS idx_kakaobot_anniv_contact ON kakaobot_anniversaries(contact_id);

-- 3. 문구 템플릿
CREATE TABLE IF NOT EXISTS kakaobot_templates (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL DEFAULT 'system',
    category VARCHAR(50) NOT NULL,
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,
    tone VARCHAR(20) DEFAULT 'friendly',
    tags JSONB DEFAULT '[]',
    use_count INTEGER DEFAULT 0,
    is_system BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kakaobot_tpl_user ON kakaobot_templates(user_id);
CREATE INDEX IF NOT EXISTS idx_kakaobot_tpl_category ON kakaobot_templates(category);

-- 4. 예약 발송
CREATE TABLE IF NOT EXISTS kakaobot_scheduled (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL DEFAULT 'default',
    contact_id INTEGER NOT NULL REFERENCES kakaobot_contacts(id) ON DELETE CASCADE,
    anniversary_id INTEGER DEFAULT NULL REFERENCES kakaobot_anniversaries(id) ON DELETE SET NULL,
    template_id INTEGER DEFAULT NULL REFERENCES kakaobot_templates(id) ON DELETE SET NULL,
    message TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    sent_at TIMESTAMPTZ DEFAULT NULL,
    send_result JSONB DEFAULT '{}',
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kakaobot_sched_status ON kakaobot_scheduled(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_kakaobot_sched_user ON kakaobot_scheduled(user_id);
