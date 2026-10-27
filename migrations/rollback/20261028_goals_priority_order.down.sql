-- 20261028_goals_priority_order.sql 되돌림. 순서 값만 버린다(다른 데이터는 그대로).
ALTER TABLE goals DROP COLUMN IF EXISTS priority_order;
