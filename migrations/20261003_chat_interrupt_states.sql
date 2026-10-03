-- 작업 중 추가 지시의 durable 상태 (RECEIVED→QUEUED→APPLIED→WORKING→DONE).
-- chat_messages.intent 는 기존 값 그대로 두고, 상태·시각·대기 사유는 여기에 쌓는다.
-- Idempotent: 재실행해도 같은 결과.

CREATE TABLE IF NOT EXISTS chat_interrupt_states (
    message_id      UUID PRIMARY KEY REFERENCES chat_messages(id) ON DELETE CASCADE,
    session_id      UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    state           TEXT NOT NULL
        CHECK (state IN ('RECEIVED', 'QUEUED', 'APPLIED', 'WORKING', 'DONE', 'CANCELLED', 'EXPIRED')),
    wait_reason     TEXT NOT NULL DEFAULT 'none'
        CHECK (wait_reason IN ('tool_running', 'relay_wait', 'model_pre_output', 'none')),
    summary         TEXT NOT NULL DEFAULT '',
    public_reply    TEXT,
    idempotency_key TEXT,
    execution_id    UUID,
    generation_id   UUID,
    applied_execution_id UUID,
    owner_instance  TEXT,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    queued_at       TIMESTAMPTZ,
    applied_at      TIMESTAMPTZ,
    working_at      TIMESTAMPTZ,
    done_at         TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 반영한 실행. WORKING/DONE 전이를 같은 세션의 다른 실행과 구분한다.
ALTER TABLE chat_interrupt_states ADD COLUMN IF NOT EXISTS applied_execution_id UUID;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_interrupt_states_idem
    ON chat_interrupt_states (session_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_chat_interrupt_states_session
    ON chat_interrupt_states (session_id, received_at DESC);
