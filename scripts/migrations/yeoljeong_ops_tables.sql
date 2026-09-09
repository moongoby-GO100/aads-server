CREATE TABLE IF NOT EXISTS yeoljeong_approvals (
    id TEXT PRIMARY KEY DEFAULT 'apv-' || substr(md5(random()::text), 1, 12),
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    approval_type TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    requested_by TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    priority TEXT DEFAULT 'normal',
    decided_by TEXT DEFAULT '',
    decided_at TIMESTAMPTZ,
    memo TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS yeoljeong_notifications (
    id TEXT PRIMARY KEY DEFAULT 'ntf-' || substr(md5(random()::text), 1, 12),
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    target_user TEXT DEFAULT '',
    notification_type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    reference_type TEXT DEFAULT '',
    reference_id TEXT DEFAULT '',
    is_read BOOLEAN DEFAULT FALSE,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS yeoljeong_audit_logs (
    id TEXT PRIMARY KEY DEFAULT 'aud-' || substr(md5(random()::text), 1, 12),
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT DEFAULT '',
    details JSONB DEFAULT '{}',
    ip_address TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yeoljeong_approvals_status ON yeoljeong_approvals (business_id, status);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_approvals_type ON yeoljeong_approvals (business_id, approval_type);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_notifications_target ON yeoljeong_notifications (business_id, target_user, is_read);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_audit_logs_business ON yeoljeong_audit_logs (business_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_audit_logs_actor ON yeoljeong_audit_logs (actor);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_audit_logs_action ON yeoljeong_audit_logs (action);
