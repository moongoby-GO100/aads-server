-- 157_goal_control_loop_dedup_and_advance.sql
-- Goal Control Loop runtime alignment.
-- The service uses ON CONFLICT (goal_id, task_type, task_id) for idempotent
-- task linking, so the database must expose the matching unique index.

CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_task_links_goal_task
    ON goal_task_links(goal_id, task_type, task_id)
    WHERE goal_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_milestones_goal_status_order
    ON milestones(goal_id, status, sequence_order);
