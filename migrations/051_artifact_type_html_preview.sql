-- 051: chat_artifacts.type CHECK 제약에 html_preview, text 추가
-- AADS-HTML-PREVIEW Phase A — AI 생성 HTML 미리보기 아티팩트 저장 지원
ALTER TABLE chat_artifacts DROP CONSTRAINT IF EXISTS chat_artifacts_type_check;
ALTER TABLE chat_artifacts ADD CONSTRAINT chat_artifacts_type_check
    CHECK (type IN (
        'report', 'code', 'chart', 'dashboard', 'table',
        'image', 'file', 'full_response', 'text', 'html_preview'
    ));
