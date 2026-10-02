-- session_relay 에 대상 실행 id 를 남긴다.
--
-- 왜 (2026-10-02, AADS-SESSION-RELAY-RESUME-ORPHAN-20261002)
--   relay 9fe3c45c·b9c0ff35 가 대상 세션의 답이 완성됐는데도 pending 으로 남아
--   물어본 세션에 회신이 가지 않았다. `ask()` 가 띄운 `_run_relay` 는 프로세스
--   안 태스크라 블루그린 컷오버로 사라지고, 대상 응답은 execution_resume 이
--   다른 프로세스에서 같은 execution_id 로 이어 써 완성했다.
--   relay 행이 그 execution_id 를 모르면 아무도 답을 되짚지 못한다.
--
--   `_run_relay` 가 스트림에서 execution_id 를 잡는 즉시 이 컬럼에 쓰고,
--   `dispatch_queued_relays` 주기(30초)의 고아 회수가 이 값으로 답을 수거한다.
--
-- 적용 순서: 코드는 이 컬럼이 없어도 동작한다(relay_tag 경로로 대체). 릴리스 때 적용.
-- 되돌리기: 컬럼은 DROP 하지 않는다. 코드가 NULL 을 허용하므로 남겨 두어도 무해하다.

ALTER TABLE session_relay ADD COLUMN IF NOT EXISTS target_execution_id uuid;

-- 고아 회수는 오래된 pending 만 본다. 그 조회를 받쳐 준다.
CREATE INDEX IF NOT EXISTS idx_session_relay_pending
    ON session_relay (created_at)
    WHERE status = 'pending';
