-- OHVIS 3-Tier: chat_artifacts에 task_card 타입 추가
-- 적용일: 2026-07-25
-- 원인: ohvis_task_manager._save_task_card()가 type='task_card'로 저장하나
--       check constraint에 누락되어 항상 실패하던 버그 수정

ALTER TABLE chat_artifacts DROP CONSTRAINT IF EXISTS chat_artifacts_type_check;
ALTER TABLE chat_artifacts ADD CONSTRAINT chat_artifacts_type_check
  CHECK (type IN (
    'report','code','chart','dashboard','table',
    'image','video','file','full_response','text',
    'html_preview','task_card'
  ));
