-- AADS-CHAT-HIDDEN-RECOVERABLE-REPLIES-P0-20260821
-- Keep real assistant replies visible even when a recovery path tagged them as
-- runner_response/interrupted_partial/_archived_partial/pipeline_runner.
-- Short system notices stay hidden. Idempotent and non-destructive.

CREATE OR REPLACE FUNCTION set_chat_message_is_hidden()
RETURNS trigger AS $$
DECLARE
    _sys boolean;
    _head text;
    _intent text;
    _len int;
    _looks_runner_notice boolean;
BEGIN
    _sys := COALESCE(NEW.model_used, '') = '';
    _head := left(ltrim(COALESCE(NEW.content, '')), 240);
    _intent := COALESCE(NEW.intent, '');
    _len := length(COALESCE(NEW.content, ''));
    _looks_runner_notice :=
        _head LIKE '%[Pipeline Runner]%'
        OR _head LIKE '%[Runner]%'
        OR _head LIKE '%[CEO 승인 요청]%'
        OR _head LIKE '%[Claude Code 작업 완료]%'
        OR (_len <= 800 AND COALESCE(NEW.content, '') LIKE '%pipeline_runner_approve%')
        OR (_len <= 800 AND COALESCE(NEW.content, '') LIKE '%검수 요청%')
        OR (_len <= 800 AND COALESCE(NEW.content, '') LIKE '%검수 실패%')
        OR (_len <= 800 AND COALESCE(NEW.content, '') LIKE '%재작업 완료%');

    NEW.is_hidden :=
        _intent IN (
            'system_trigger',
            'pipeline_c_start',
            'pipeline_c_result',
            'pipeline_c',
            'auto_reaction',
            'streaming_placeholder',
            'interruption_notice',
            'runner_notification',
            'ai_review_warning',
            '_deleted_duplicate'
        )
        OR (_intent = 'pipeline_runner' AND (_sys OR _looks_runner_notice))
        OR (_intent IN ('runner_response', 'interrupted_partial', '_archived_partial') AND _len <= 200)
        OR (NEW.role = 'assistant' AND _sys AND _looks_runner_notice)
        OR (NEW.role = 'user' AND COALESCE(NEW.content, '') LIKE '[시스템]%');

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chat_messages_is_hidden ON chat_messages;

CREATE TRIGGER trg_chat_messages_is_hidden
BEFORE INSERT OR UPDATE OF role, content, intent, model_used
ON chat_messages
FOR EACH ROW
EXECUTE FUNCTION set_chat_message_is_hidden();

UPDATE chat_messages
SET is_hidden = FALSE
WHERE is_hidden = TRUE
  AND role = 'assistant'
  AND COALESCE(intent, '') IN ('runner_response', 'interrupted_partial', '_archived_partial')
  AND length(COALESCE(content, '')) > 200;

UPDATE chat_messages
SET is_hidden = FALSE
WHERE is_hidden = TRUE
  AND role = 'assistant'
  AND COALESCE(intent, '') = 'pipeline_runner'
  AND COALESCE(model_used, '') NOT IN ('', 'streaming', 'interrupted')
  AND length(COALESCE(content, '')) > 800
  AND NOT (
    left(ltrim(COALESCE(content, '')), 240) LIKE '%[Pipeline Runner]%'
    OR left(ltrim(COALESCE(content, '')), 240) LIKE '%[Runner]%'
    OR left(ltrim(COALESCE(content, '')), 240) LIKE '%[CEO 승인 요청]%'
    OR left(ltrim(COALESCE(content, '')), 240) LIKE '%[Claude Code 작업 완료]%'
  );

SELECT COALESCE(intent, '(null)') AS intent,
       is_hidden,
       count(*) AS cnt
FROM chat_messages
WHERE role = 'assistant'
  AND COALESCE(intent, '') IN ('runner_response', 'interrupted_partial', '_archived_partial', 'pipeline_runner')
  AND length(COALESCE(content, '')) > 200
GROUP BY 1, 2
ORDER BY 1, 2;
