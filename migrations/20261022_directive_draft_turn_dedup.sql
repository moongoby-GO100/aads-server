-- AADS-M7-DRAFT-TURN-DEDUP-R2-20261022
-- 같은 턴(tenant_id + 선택한 AI 응답 message id)에서 지시 초안이 중복 생성되던 문제를 DB 가 막는다.
-- 삭제 없음: 기존 중복은 한 행만 활성으로 두고 나머지는 classification.superseded_by 로 표시한다.
-- directive_drafts.status CHECK 는 draft/approved/rejected/sent/archived 뿐이라 'superseded' 값이 없다.
-- 그래서 draft 였던 행만 'archived' 로 바꾸고, 이미 sent/approved/rejected 인 행의 status 는 건드리지 않는다
-- (전달 이력을 잃지 않기 위해). 원래 status 는 superseded_prev_status 에 남겨 down 에서 복원한다.
-- 활성 행 선택: 이미 sent/approved 된 행 우선, 그다음 최신(created_at DESC, id DESC).
-- 멱등: 재실행 시 superseded_by 가 있는 행은 대상에서 빠지고 인덱스는 IF NOT EXISTS.

BEGIN;

WITH ranked AS (
    SELECT id,
           first_value(id) OVER w AS keeper_id,
           row_number() OVER w AS rn
      FROM directive_drafts
     WHERE classification->>'source_mode' = 'selected_response'
       AND classification->>'selected_assistant_message_id' IS NOT NULL
       AND classification->>'superseded_by' IS NULL
    WINDOW w AS (
        PARTITION BY tenant_id, classification->>'selected_assistant_message_id'
        ORDER BY (status IN ('sent', 'approved')) DESC, created_at DESC, id DESC
    )
)
UPDATE directive_drafts d
   SET status = CASE WHEN d.status = 'draft' THEN 'archived' ELSE d.status END,
       classification = d.classification || jsonb_build_object(
           'superseded_by', r.keeper_id::text,
           'superseded_reason', 'selected_response_turn_dedup',
           'superseded_prev_status', d.status
       )
  FROM ranked r
 WHERE d.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS ux_directive_drafts_selected_response_turn
    ON directive_drafts (tenant_id, (classification->>'selected_assistant_message_id'))
    WHERE classification->>'source_mode' = 'selected_response'
      AND classification->>'selected_assistant_message_id' IS NOT NULL
      AND classification->>'superseded_by' IS NULL;

COMMIT;
