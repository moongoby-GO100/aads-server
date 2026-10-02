-- AADS-CANONICAL-GATE-SHADOW-20261002 — 정본 강제 게이트 그림자 모드 기록 테이블.
-- 적용은 머지 후 별도 승인 단계에서 한다. 이 테이블이 없어도 게이트는 로그만 남기고 통과한다(fail-open).
-- 롤백: migrations/rollback/20261002_canonical_gate_events.down.sql
BEGIN;

CREATE TABLE IF NOT EXISTS canonical_gate_events (
    id          bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    entrypoint  text NOT NULL CHECK (entrypoint IN ('goal_api', 'commit', 'runner_submit')),
    project     text,
    ref         text NOT NULL,
    verdict     text NOT NULL CHECK (verdict IN ('ok', 'missing_canonical', 'unknown')),
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    mode        text NOT NULL CHECK (mode IN ('shadow', 'enforce'))
);

CREATE INDEX IF NOT EXISTS canonical_gate_events_occurred_idx
    ON canonical_gate_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS canonical_gate_events_entry_verdict_idx
    ON canonical_gate_events (entrypoint, verdict, occurred_at DESC);

COMMIT;
