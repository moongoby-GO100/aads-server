-- 20261023_goals_owner_required_check.sql 되돌림. 제약만 내린다(데이터는 그대로).
ALTER TABLE goals DROP CONSTRAINT IF EXISTS ck_goals_owner_required;
