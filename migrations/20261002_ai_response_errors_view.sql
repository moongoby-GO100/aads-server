-- AI 응답 오류 통합 조회 VIEW (읽기 전용). 테이블/데이터는 건드리지 않는다.
--
-- kind 5종 (서로 겹치지 않는다):
--   fallback_exhausted : error_log 의 claude_api_fallback (계정 교차 폴백이 모두 실패)
--   llm_outage         : assistant 행 중 content 가 '⚠️ _전체 LLM 장애' 로 시작하는 것 (접두 일치만)
--   interrupted        : assistant 행 중 model_used='interrupted' 이면서 llm_outage 가 아닌 것
--                        (사용자 중단 등 — 장애가 아니다)
--   low_quality        : assistant 행 중 quality_score < 0.4
--   error_book_chat    : ohvis_wiki_error_book 의 error_key LIKE 'chat.%'
--
-- 2026-10-02 실측: model_used='interrupted' 2,153건 중 2,125건은 장애가 아닌 중단 턴이었다.
-- 그래서 llm_outage 는 접두 일치만 쓰고 interrupted 는 별도 kind 로 분리한다.
--
-- 되돌리기: 20261002_ai_response_errors_view_rollback.sql

CREATE OR REPLACE VIEW ai_response_errors AS
SELECT 'fallback_exhausted'::text AS kind,
       COALESCE(e.last_seen, e.created_at)::timestamptz AS occurred_at,
       'error_log'::text AS source_table,
       e.id::text AS source_id,
       NULL::uuid AS session_id,
       NULL::text AS model_used,
       LEFT(e.message, 300)::text AS summary,
       jsonb_build_object(
           'error_hash', e.error_hash,
           'error_type', e.error_type,
           'source', e.source,
           'occurrence_count', e.occurrence_count,
           'first_seen', e.first_seen,
           'resolution_type', e.resolution_type
       ) AS detail,
       (SELECT MIN(b.error_key)
          FROM ohvis_wiki_error_book b
         WHERE b.metadata ->> 'error_hash' = e.error_hash
           AND b.error_key LIKE 'chat.%'
        HAVING COUNT(DISTINCT b.error_key) = 1)::text AS error_book_key
  FROM error_log e
 WHERE e.error_type = 'claude_api_fallback'
UNION ALL
SELECT 'llm_outage'::text AS kind,
       m.created_at AS occurred_at,
       'chat_messages'::text AS source_table,
       m.id::text AS source_id,
       m.session_id AS session_id,
       m.model_used::text AS model_used,
       LEFT(m.content, 300)::text AS summary,
       jsonb_build_object('intent', m.intent, 'tokens_out', m.tokens_out) AS detail,
       NULL::text AS error_book_key
  FROM chat_messages m
 WHERE m.role = 'assistant'
   AND starts_with(m.content, '⚠️ _전체 LLM 장애')
UNION ALL
SELECT 'interrupted'::text AS kind,
       m.created_at AS occurred_at,
       'chat_messages'::text AS source_table,
       m.id::text AS source_id,
       m.session_id AS session_id,
       m.model_used::text AS model_used,
       LEFT(m.content, 300)::text AS summary,
       jsonb_build_object('intent', m.intent, 'tokens_out', m.tokens_out) AS detail,
       NULL::text AS error_book_key
  FROM chat_messages m
 WHERE m.role = 'assistant'
   AND m.model_used = 'interrupted'
   AND NOT starts_with(m.content, '⚠️ _전체 LLM 장애')
UNION ALL
SELECT 'low_quality'::text AS kind,
       m.created_at AS occurred_at,
       'chat_messages'::text AS source_table,
       m.id::text AS source_id,
       m.session_id AS session_id,
       m.model_used::text AS model_used,
       LEFT(m.content, 300)::text AS summary,
       jsonb_build_object('quality_score', m.quality_score, 'intent', m.intent) AS detail,
       NULL::text AS error_book_key
  FROM chat_messages m
 WHERE m.role = 'assistant'
   AND m.quality_score < 0.4
UNION ALL
SELECT 'error_book_chat'::text AS kind,
       COALESCE(b.updated_at, b.created_at) AS occurred_at,
       'ohvis_wiki_error_book'::text AS source_table,
       b.id::text AS source_id,
       NULL::uuid AS session_id,
       NULL::text AS model_used,
       LEFT(b.symptom, 300)::text AS summary,
       jsonb_build_object(
           'project', b.project,
           'status', b.status,
           'recurrence_count', b.recurrence_count
       ) AS detail,
       b.error_key::text AS error_book_key
  FROM ohvis_wiki_error_book b
 WHERE b.error_key LIKE 'chat.%';

COMMENT ON VIEW ai_response_errors IS
    'AI 응답 오류 통합 조회(읽기 전용). kind: fallback_exhausted/llm_outage/interrupted/low_quality/error_book_chat.';
