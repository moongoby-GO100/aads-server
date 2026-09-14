-- 180_chat_generation_terminal_settlement.sql
--
-- 결함: 실행(chat_turn_executions)이 종결돼도 generation 은 계속 'active' 로 남는다.
-- 실측(2026-09-14 10:34 KST): ended_at IS NULL 인 generation 131건 중 119건이
-- 30분 초과 정체. 이미 completed 된 실행의 generation 조차 active 였다.
--
-- 원인: 종결 책임이 app/services/chat_commands.py::settle_generation() 에만 있는데
-- 프로덕션 호출부가 없다(테스트에서만 호출). 생성은 트리거
-- trg_chat_execution_a_generation 이 담당하므로 생성/종결 소유자가 갈려 있었다.
-- 게다가 종결해야 할 시점은 대개 슬롯 교체·프로세스 소멸 직후라, 앱 프로세스에
-- 종결을 맡기면 구조적으로 놓칠 수밖에 없다.
--
-- 조치: 종결을 생성과 같은 DB 계층으로 옮긴다. 실행이 종결 상태로 전이하면
-- 최신 epoch 의 active generation 을 같은 트랜잭션에서 닫는다.
--
-- 안전성:
--   - 기존 fence 트리거(aads_chat_fence_generation)가 active → completed/failed
--     전이를 허용하고 ended_at 을 자동 스탬프한다. 계약 위반 없음.
--   - 재개는 _claim_execution_lease() 가 항상 owner_epoch+1 을 하므로 새 epoch 의
--     새 generation 이 생긴다. 닫힌 generation 을 되살리지 않는다.
--   - chat_turn_executions 를 쓰지 않으므로 재귀나 revision 원장 팽창이 없다.
--
-- 롤백: DROP TRIGGER trg_chat_execution_settle_generation ON chat_turn_executions;
--       (백필된 행은 이미 종결 상태이며 되돌릴 필요가 없다 — 읽기 경로는
--        terminal generation 을 정상 처리한다.)

BEGIN;

CREATE OR REPLACE FUNCTION aads_chat_settle_generation() RETURNS TRIGGER AS $$
DECLARE
    v_status TEXT;
BEGIN
    IF NEW.status NOT IN ('completed', 'failed', 'error', 'interrupted', 'cancelled') THEN
        RETURN NULL;
    END IF;

    v_status := CASE WHEN NEW.status = 'completed' THEN 'completed' ELSE 'failed' END;

    -- 최신 epoch 만 닫는다. 구 epoch 은 aads_chat_ensure_generation() 이
    -- 이미 'superseded' 로 처리했다.
    UPDATE chat_execution_generations g
    SET status = v_status,
        ended_at = COALESCE(g.ended_at, NOW())
    WHERE g.execution_id = NEW.id
      AND g.status = 'active'
      AND NOT EXISTS (
          SELECT 1
          FROM chat_execution_generations peer
          WHERE peer.execution_id = g.execution_id
            AND peer.owner_epoch > g.owner_epoch
      );

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chat_execution_settle_generation ON chat_turn_executions;

-- 이름이 trg_chat_execution_a_generation 보다 뒤이므로 생성 트리거가 먼저 돈다.
CREATE TRIGGER trg_chat_execution_settle_generation
AFTER UPDATE OF status ON chat_turn_executions
FOR EACH ROW
WHEN (OLD.status IS DISTINCT FROM NEW.status)
EXECUTE FUNCTION aads_chat_settle_generation();

-- 백필: 실행이 이미 종결된 generation 만 닫는다. 살아 있는 실행은 건드리지 않는다.
UPDATE chat_execution_generations g
SET status = CASE WHEN e.status = 'completed' THEN 'completed' ELSE 'failed' END,
    ended_at = COALESCE(g.ended_at, e.completed_at, NOW())
FROM chat_turn_executions e
WHERE e.id = g.execution_id
  AND g.status = 'active'
  AND e.status IN ('completed', 'failed', 'error', 'interrupted', 'cancelled')
  AND NOT EXISTS (
      SELECT 1
      FROM chat_execution_generations peer
      WHERE peer.execution_id = g.execution_id
        AND peer.owner_epoch > g.owner_epoch
  );

COMMIT;
