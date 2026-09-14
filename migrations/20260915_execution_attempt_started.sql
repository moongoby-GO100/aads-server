-- 재시도해도 나이 시계가 리셋되지 않아 와치독이 살아 있는 턴을 죽였다.
--
-- 2026-09-15 08:42, #310 주도 세션이 그렇게 잘렸다. 배포 전환으로 턴이
-- 여섯 번 갈아탔는데(owner_epoch=6, retry_count=5) `started_at` 은 첫 시도
-- 시각 그대로라, 누적 1348초가 opus-5 상한 1200초를 넘겼다. 상한은 **한 번
-- 멈춘 턴**을 잡으려던 것인데 **여섯 번 다시 시작한 턴**에 적용됐다.
--
-- 이 컬럼은 "지금 돌고 있는 시도가 언제 시작했나" 다. 와치독은 이것으로
-- 재고, 전체 나이(started_at)는 절대 상한에만 쓴다.
ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS attempt_started_at timestamptz;

UPDATE chat_turn_executions
SET attempt_started_at = COALESCE(started_at, created_at)
WHERE attempt_started_at IS NULL;
