-- 20261022 POST /api/v1/goals/triage/apply 멱등 저장소 (PRD goal-first-triage-execution-prd TR-4)
--
-- 같은 (tenant_id, idempotency_key) 재전송에 **같은 응답**을 돌려주기 위해 성공한 실행의 응답을 보관한다.
-- 실패(409/403/422)는 아무것도 바꾸지 않으므로 저장하지 않는다 — 재시도는 새로 판정한다.
-- request_hash 는 같은 키에 다른 본문이 오는 오용을 잡는 용도다.
-- 되돌림: migrations/rollback/20261022_goal_triage_runs.down.sql
-- 멱등: 여러 번 적용해도 안전하다. 기존 테이블은 건드리지 않는다.
CREATE TABLE IF NOT EXISTS goal_triage_runs (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    request_hash    TEXT NOT NULL,
    actor           TEXT,
    response        JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_goal_triage_runs_key UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_goal_triage_runs_created ON goal_triage_runs (tenant_id, created_at DESC);
