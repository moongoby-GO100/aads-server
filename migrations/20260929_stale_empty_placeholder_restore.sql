-- M5 부분 응답 보존 (AADS-LLM-M5-PARTIAL-PRESERVE-20260929). 데이터 복구, 멱등.
-- 예전 스윕은 "trim 후 50자 미만" 만으로 assistant 행을 숨겼다. 그 결과
-- 정상 완료된 짧은 답·보존된 부분 응답이 stale_empty_placeholder 로 숨겨졌다.
-- 본문이 남아 있는 행만 다시 보이게 한다. 본문이 빈 행은 그대로 둔다.
-- 원래 intent 는 남아 있지 않으므로 복원하지 않는다(추측 금지).
-- 롤백: migrations/rollback/20260929_stale_empty_placeholder_restore.down.sql
UPDATE chat_messages
SET is_hidden = FALSE,
    edited_at = NOW()
WHERE intent = 'stale_empty_placeholder'
  AND is_hidden = TRUE
  AND role = 'assistant'
  AND deleted_at IS NULL
  AND length(trim(COALESCE(content, ''))) > 0;
