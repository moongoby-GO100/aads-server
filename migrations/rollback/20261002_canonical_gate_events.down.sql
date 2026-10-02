-- Down migration for migrations/20261002_canonical_gate_events.sql
-- 게이트 기록만 지운다. 정본·goal_documents 는 건드리지 않는다.
DROP TABLE IF EXISTS canonical_gate_events;
