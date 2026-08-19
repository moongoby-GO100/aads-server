-- AADS-IS-HIDDEN-FP-P0-20260819
-- 문제: 120번 트리거가 content 전체를 '%[Pipeline Runner]%' / '%[Runner]%' 로 부분매칭하여
--       해당 문자열을 "본문 중간에 인용한" 정상 CEO 최종보고(수만 자)까지 is_hidden=TRUE 로 만들어
--       채팅창에서 통째로 사라지게 함. (실측: 36,885자 보고서가 position 14042 매칭으로 숨김 처리)
-- 조치: ①마커가 메시지 "선두"(<=8자)에 있을 때만 ②LLM 응답이 아닌 시스템 발신(model_used IS NULL)일 때만 숨김
--       ③기존 오탐 행 복구
-- 비파괴/멱등.

CREATE OR REPLACE FUNCTION set_chat_message_is_hidden()
RETURNS trigger AS $$
DECLARE
    _is_system_generated boolean;
    _head text;
BEGIN
    -- 러너/시스템 알림은 LLM 모델을 거치지 않으므로 model_used 가 비어 있다.
    _is_system_generated := COALESCE(NEW.model_used, '') = '';
    _head := left(COALESCE(NEW.content, ''), 40);

    NEW.is_hidden :=
        COALESCE(NEW.intent, '') IN (
            'system_trigger',
            'pipeline_c_start',
            'pipeline_c_result',
            'pipeline_c',
            'auto_reaction',
            'streaming_placeholder',
            'interrupted_partial',
            'interruption_notice',
            '_archived_partial',
            'runner_response',
            'pipeline_runner',
            'runner_notification',
            'ai_review_warning',
            '_deleted_duplicate'
        )
        OR (
            NEW.role = 'assistant'
            AND _is_system_generated
            AND (_head LIKE '%[Pipeline Runner]%' OR _head LIKE '%[Runner]%')
        )
        OR (NEW.role = 'user' AND COALESCE(NEW.content, '') LIKE '[시스템]%');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chat_messages_is_hidden ON chat_messages;

CREATE TRIGGER trg_chat_messages_is_hidden
BEFORE INSERT OR UPDATE OF role, content, intent
ON chat_messages
FOR EACH ROW
EXECUTE FUNCTION set_chat_message_is_hidden();

-- 오탐 복구: 실제 LLM이 생성한 assistant 응답인데 숨김 처리된 행
UPDATE chat_messages
SET is_hidden = FALSE
WHERE is_hidden = TRUE
  AND role = 'assistant'
  AND COALESCE(model_used, '') NOT IN ('', 'streaming', 'interrupted')
  AND COALESCE(intent, '') NOT IN (
        'system_trigger',
        'pipeline_c_start',
        'pipeline_c_result',
        'pipeline_c',
        'auto_reaction',
        'streaming_placeholder',
        'interrupted_partial',
        'interruption_notice',
        '_archived_partial',
        'runner_response',
        'pipeline_runner',
        'runner_notification',
        'ai_review_warning',
        '_deleted_duplicate'
  );
