-- 20261028 goals.priority_order — 목표 처리 순서(작을수록 먼저, 테넌트 단위 전체 순서, NULL 은 맨 뒤).
-- main.py lifespan 이 같은 ADD COLUMN IF NOT EXISTS 를 보장하고, 초기값은
-- app/services/goal_priority_order.py backfill_priority_order() 가 멱등으로 채운다.
-- 되돌림: migrations/rollback/20261028_goals_priority_order.down.sql
ALTER TABLE goals ADD COLUMN IF NOT EXISTS priority_order integer;
