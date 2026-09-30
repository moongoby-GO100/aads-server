-- AADS-GOAL-STATUS-WHITELIST-AUDIT-20260930
-- goals.status 변경 감사기록. 2026-09-30 NTV2 목표가 status='paused'(paused_at NULL)로
-- 직접 바뀌었는데 누가 바꿨는지 추적할 수단이 없었다.
-- 쓰는 곳: app/services/goal_manager.py GoalStateMachine.update_goal() (status 가 실제로 바뀔 때만 1행)
-- 멱등: 여러 번 적용해도 안전하다. 기존 테이블은 건드리지 않는다.

CREATE TABLE IF NOT EXISTS goal_status_audit (
    id          BIGSERIAL PRIMARY KEY,
    goal_id     UUID NOT NULL,
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor       TEXT,   -- 'chat_session:<id>' | 'user:<id>' | NULL(알 수 없음)
    source      TEXT,   -- 'update_goal' 등 경로 이름
    tenant_id   UUID,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_goal_status_audit_goal_changed
    ON goal_status_audit (goal_id, changed_at DESC);
