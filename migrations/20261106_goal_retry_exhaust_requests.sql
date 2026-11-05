-- 재작업 소진 주도 판단 요청 장부.
-- 목적: 재작업 실행 한도(2회)를 다 쓴 마일스톤에서 "사람이 확인해야 한다" 로만 끝나던 것을
--       주도 세션 -> (무응답 시) 대표님 순서로 올리되, 소진 시점에는 이 표에 행 1개만 남기고
--       발송은 커밋 뒤 별도 스윕(app/services/goal_retry_exhaust_triage.py)이 한다.
-- first_attempt_at: 첫 발송 시도 직전에 기록. 무응답 판정 기준은 COALESCE(first_attempt_at, sent_at).
-- 멱등: 두 번 실행해도 오류가 없다(IF NOT EXISTS).
-- 되돌림: migrations/rollback/20261106_goal_retry_exhaust_requests.down.sql (DROP TABLE).
--         코드는 이 표가 없으면 기록·스윕을 조용히 건너뛰므로 표를 먼저 지워도 안전하다.
BEGIN;
CREATE TABLE IF NOT EXISTS goal_retry_exhaust_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    milestone_id uuid NOT NULL,
    lineage text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('lead', 'ceo')),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'resolved', 'escalated', 'undeliverable')),
    target_session_id uuid,
    milestone_status_at_request text,
    executions int,
    attempts int NOT NULL DEFAULT 0,
    last_error text,
    delivery_ref text,
    first_attempt_at timestamptz,
    sent_at timestamptz,
    resolved_at timestamptz,
    resolution text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, milestone_id, lineage, kind)
);
CREATE INDEX IF NOT EXISTS idx_goal_retry_exhaust_requests_kind_status
    ON goal_retry_exhaust_requests (kind, status, created_at);
ALTER TABLE goal_retry_exhaust_requests ADD COLUMN IF NOT EXISTS first_attempt_at timestamptz;
COMMIT;
