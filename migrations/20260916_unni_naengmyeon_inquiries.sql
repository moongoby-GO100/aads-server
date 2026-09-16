BEGIN;

CREATE TABLE IF NOT EXISTS unni_naengmyeon_inquiries (
    id BIGSERIAL PRIMARY KEY,
    reference UUID NOT NULL UNIQUE,
    name VARCHAR(50) NOT NULL,
    contact VARCHAR(100) NOT NULL,
    subject VARCHAR(100) NOT NULL DEFAULT '일반 문의',
    message TEXT NOT NULL CHECK (char_length(message) BETWEEN 10 AND 2000),
    privacy_consent BOOLEAN NOT NULL CHECK (privacy_consent IS TRUE),
    status VARCHAR(20) NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'in_progress', 'answered', 'closed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_unni_naengmyeon_inquiries_status_created
    ON unni_naengmyeon_inquiries (status, created_at DESC);

COMMENT ON TABLE unni_naengmyeon_inquiries IS
    '언니냉면 홈페이지 비공개 고객 문의. 연락처 포함으로 공개 조회 API를 제공하지 않는다.';

COMMIT;
