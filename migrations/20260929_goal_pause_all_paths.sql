-- 목표 단위 멈춤 (AADS-GOAL-PAUSE-ALL-PATHS, 2026-09-29). additive only.
-- 롤백: migrations/rollback/20260929_goal_pause_all_paths.down.sql
ALTER TABLE goals ADD COLUMN IF NOT EXISTS paused_at timestamptz;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS paused_reason text;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS paused_by text;
-- 릴레이를 목표에 묶는다. 목표가 지워지면 릴레이 기록은 남기고 연결만 끊는다
-- (ON DELETE SET NULL — 목표 삭제가 릴레이 FK 로 막히지 않게).
ALTER TABLE session_relay ADD COLUMN IF NOT EXISTS goal_id uuid
    REFERENCES goals(id) ON DELETE SET NULL;
ALTER TABLE session_relay ADD COLUMN IF NOT EXISTS pending_reply text;
