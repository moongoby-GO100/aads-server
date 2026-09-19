-- Project별 작업 중/완료 미확인 세션 통합함.
-- 사용자 최초 조회 시점을 기준선으로 삼아 배포 전 완료 이력은 노출하지 않는다.

CREATE TABLE IF NOT EXISTS chat_session_attention_users (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS chat_session_attention_acknowledgements (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    acknowledged_execution_id UUID REFERENCES chat_turn_executions(id) ON DELETE SET NULL,
    acknowledged_completed_at TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, session_id)
);

-- 기존 사용자는 기능 배포 시점을 기준선으로 갖는다. 이후 생성된 사용자는 첫 조회
-- 시 서비스 레이어가 기준선을 만든다.
INSERT INTO chat_session_attention_users (tenant_id, user_id)
SELECT tenant_id, user_id
FROM tenant_memberships
WHERE status = 'active'
ON CONFLICT (tenant_id, user_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_chat_session_attention_ack_user_updated
    ON chat_session_attention_acknowledgements (tenant_id, user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_turn_executions_session_completed_attention
    ON chat_turn_executions (session_id, completed_at DESC)
    WHERE status = 'completed' AND completed_at IS NOT NULL;
