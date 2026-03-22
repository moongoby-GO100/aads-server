-- AADS P2-2: 대화 분기(Branch) 기능
-- 메시지에 분기 추적 컬럼 추가
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS branch_id UUID DEFAULT NULL;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS branch_point_id UUID DEFAULT NULL;
-- branch_point_id: 이 메시지가 분기된 원본 메시지의 id
-- branch_id: 같은 분기에 속하는 메시지들의 그룹 id

CREATE INDEX IF NOT EXISTS idx_messages_branch ON chat_messages (branch_id) WHERE branch_id IS NOT NULL;
