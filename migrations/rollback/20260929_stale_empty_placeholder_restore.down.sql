-- 20260929_stale_empty_placeholder_restore.sql 롤백.
-- 복구 시각 이후 edited_at 이 찍힌 본문 있는 행만 다시 숨긴다.
-- :restored_at 에 복구 실행 시각을 넣어 실행한다.
UPDATE chat_messages
SET is_hidden = TRUE
WHERE intent = 'stale_empty_placeholder'
  AND is_hidden = FALSE
  AND role = 'assistant'
  AND length(trim(COALESCE(content, ''))) > 0
  AND edited_at >= :'restored_at'::timestamptz;
