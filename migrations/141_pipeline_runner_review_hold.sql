-- 141: Pipeline Runner review infrastructure failures use FLAG+hold.
-- Date: 2026-09-02
--
-- Goal:
-- - Stop review infrastructure failures from entering awaiting_approval.
-- - Preserve historical infra-failure rows as review_hold action-required items.

UPDATE pipeline_jobs
SET
    status = 'review_hold',
    phase = 'review_hold',
    error_detail = regexp_replace(
        COALESCE(error_detail, 'review_infra_failed'),
        '^review_failed:',
        'review_infra_failed:'
    ),
    review_feedback = COALESCE(review_feedback, '')
        || E'\n[Runner Guard] FLAG+hold: AI 리뷰 인프라 장애는 코드 반려가 아니며 승인 대기 유입을 차단함',
    updated_at = NOW()
WHERE (
        phase = 'review_failed'
        OR phase = 'review_hold'
        OR error_detail LIKE 'review_infra_failed%'
        OR error_detail LIKE 'review_failed:%REVIEW_API_UNAVAILABLE%'
        OR error_detail LIKE 'review_failed:%REVIEW_MODEL_NO_RESPONSE%'
        OR error_detail LIKE 'review_failed:%REVIEW_PARSER_FAILURE%'
        OR error_detail LIKE 'review_failed:%REVIEW_TIMEOUT%'
    )
  AND COALESCE(review_needs_retry, FALSE) = TRUE
  AND COALESCE(review_flag_category, '') IN (
        'REVIEW_API_UNAVAILABLE',
        'REVIEW_MODEL_NO_RESPONSE',
        'REVIEW_PARSER_FAILURE',
        'REVIEW_TIMEOUT',
        'REVIEW_SYSTEM_FAILURE'
    )
  AND status IS DISTINCT FROM 'review_hold';
