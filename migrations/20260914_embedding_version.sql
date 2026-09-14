-- 임베딩 세대 표시 — 어떤 벡터가 쓸 수 있는 것인지 구분한다.
--
-- 2026-09-14. 이 컬럼이 없어서 오늘 하루를 날렸다. chat_messages 에
-- 벡터가 33,140건 있었는데 그중 진짜는 65건이었다. 나머지는 임베딩 경로가
-- 끊긴 채로 조용히 저장된 해시 더미다. 차원도 값 분포도 진짜와 같아서
-- 겉으로는 구분이 안 된다 — 유사도 숫자도 그럴듯하게 나온다.
--
-- 결국 더미를 찾아낸 방법은 `_dummy_embedding` 이 8개 값을 768차원까지
-- 되풀이해 채운다는 구현 세부였다(1번째 원소 == 9번째 원소). 구현이
-- 조금만 달랐으면 영영 못 찾았다. 다음부터는 세대를 적어 둔다.
--
--   NULL = 세대 미상 (2026-09-14 이전. 신뢰하지 마라)
--   1    = nomic-embed-text, 접두어 없음
--   2    = nomic-embed-text, search_document:/search_query: 접두어
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS embedding_ver smallint;
ALTER TABLE memory_facts  ADD COLUMN IF NOT EXISTS embedding_ver smallint;

-- 채울 대상을 고르는 질의가 매번 전체를 훑지 않도록.
CREATE INDEX IF NOT EXISTS idx_chat_messages_embed_ver
    ON chat_messages (session_id)
    WHERE embedding_ver IS DISTINCT FROM 2;
