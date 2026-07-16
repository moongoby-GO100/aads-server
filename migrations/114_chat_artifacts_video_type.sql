-- 114: chat_artifacts에 video 타입 허용
-- 생성형 영상 결과를 채팅 아티팩트로 저장하고 대시보드에서 바로 재생하기 위한 제약 확장.
ALTER TABLE chat_artifacts DROP CONSTRAINT IF EXISTS chat_artifacts_type_check;
ALTER TABLE chat_artifacts ADD CONSTRAINT chat_artifacts_type_check
    CHECK (type IN (
        'report', 'code', 'chart', 'dashboard', 'table',
        'image', 'video', 'file', 'full_response', 'text', 'html_preview'
    ));
