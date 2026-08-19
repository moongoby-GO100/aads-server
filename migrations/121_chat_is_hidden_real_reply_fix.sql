-- AADS-BUBBLE-HIDDEN-P0-20260819
-- 문제: chat_messages.is_hidden 트리거가 'runner_response'와 'interrupted_partial'
--       intent를 무조건 숨김 처리하여, 실제 LLM이 작성한 CEO 보고 응답(최대 32,504자)이
--       채팅 화면에서 통째로 사라짐.
--       실측: runner_response 2,781건(model_used=claude-*/gpt-* 실모델) 전량 hidden,
--             interrupted_partial 267건(중단 보존 본문) 전량 hidden.
-- 조치: 두 intent는 '짧은 시스템성 흔적(<=200자)'일 때만 숨기고,
--       실제 본문이 있는 응답은 화면에 노출한다.
-- 유지: streaming_placeholder / interruption_notice / _archived_partial(대체 완료본) /
--       러너 자동알림 / [시스템] user 트리거는 기존대로 숨김 → 중복 버블 발생 없음.
-- 비파괴 · 멱등.

CREATE OR REPLACE FUNCTION set_chat_message_is_hidden()
RETURNS trigger AS $$
DECLARE
    _sys boolean;
    _head text;
    _intent text;
    _len int;
BEGIN
    _sys := COALESCE(NEW.model_used, '') = '';
    _head := left(COALESCE(NEW.content, ''), 40);
    _intent := COALESCE(NEW.intent, '');
    _len := length(COALESCE(NEW.content, ''));

    NEW.is_hidden :=
        _intent IN (
            'system_trigger',
            'pipeline_c_start',
            'pipeline_c_result',
            'pipeline_c',
            'auto_reaction',
            'streaming_placeholder',
            'interruption_notice',
            '_archived_partial',
            'pipeline_runner',
            'runner_notification',
            'ai_review_warning',
            '_deleted_duplicate'
        )
        -- 실제 본문이 있는 AI 응답은 노출, 짧은 흔적만 숨김
        OR (_intent IN ('runner_response', 'interrupted_partial') AND _len <= 200)
        OR (NEW.role = 'assistant' AND _sys AND (_head LIKE '%[Pipeline Runner]%' OR _head LIKE '%[Runner]%'))
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

-- 백필: 과거에 잘못 숨겨진 실제 AI 응답 복구 (is_hidden 단독 UPDATE는 트리거 미발동)
UPDATE chat_messages
SET is_hidden = FALSE
WHERE is_hidden = TRUE
  AND COALESCE(intent, '') IN ('runner_response', 'interrupted_partial')
  AND length(COALESCE(content, '')) > 200
  AND NOT (role = 'assistant' AND COALESCE(model_used, '') = ''
           AND (left(content, 40) LIKE '%[Pipeline Runner]%' OR left(content, 40) LIKE '%[Runner]%'));

-- 검증용 카운트
SELECT COALESCE(intent, '(null)') AS intent,
       is_hidden,
       count(*) AS cnt
FROM chat_messages
WHERE COALESCE(intent, '') IN ('runner_response', 'interrupted_partial')
GROUP BY 1, 2
ORDER BY 3 DESC;
