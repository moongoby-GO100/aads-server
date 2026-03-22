-- AADS-Reply-To: 댓글형 답글 지정 기능
-- reply_to_id 컬럼 추가 → 이전 AI 응답을 지정해서 추가 지시

ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS reply_to_id UUID REFERENCES chat_messages(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON chat_messages(reply_to_id) WHERE reply_to_id IS NOT NULL;
