-- AADS-SB-P0-WORK-RECIPE-ENGINE-20260917
-- 스마트 브라우저 P0 — 작업 레시피 스키마.
-- 추가 전용(additive only). DROP/ALTER ... DROP 없음.
--
-- 적용은 CEO 승인 후 점검 창구에서. 이 파일은 생성까지만 한다.
--
-- 테이블 4종
--   work_recipes      레시피 본문(버전별 불변 행)
--   recipe_runs       재생 1회 = 1행. llm_calls 가 재생의 증거다(AC-1).
--   recipe_run_steps  단계별 기록. append-only (UPDATE/DELETE 트리거 차단).
--   recipe_approvals  WRITE_EXTERNAL 이상 단계의 승인 요청/결정 (P1 에서 사용)

BEGIN;

-- ------------------------------------------------------------- work_recipes
CREATE TABLE IF NOT EXISTS work_recipes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    domain          TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    description     TEXT NOT NULL DEFAULT '',
    spec            JSONB NOT NULL DEFAULT '{}'::jsonb,
    yaml_source     TEXT NOT NULL DEFAULT '',
    max_risk        TEXT NOT NULL DEFAULT 'READ',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_by      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT work_recipes_version_positive CHECK (version >= 1),
    CONSTRAINT work_recipes_max_risk_valid CHECK (
        max_risk IN ('READ', 'WRITE_INTERNAL', 'WRITE_EXTERNAL', 'IRREVERSIBLE')
    )
);

-- tenant_id 가 NULL 인 전역 레시피도 같은 유니크 공간에 들어가야 한다.
-- Postgres 는 NULL 을 서로 다른 값으로 보므로 COALESCE 로 묶는다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_work_recipes_identity
    ON work_recipes (
        COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid),
        domain, name, version
    );

CREATE INDEX IF NOT EXISTS idx_work_recipes_lookup
    ON work_recipes (domain, name, version DESC)
    WHERE enabled;

-- ------------------------------------------------------------- recipe_runs
CREATE TABLE IF NOT EXISTS recipe_runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NULL REFERENCES tenants(id) ON DELETE CASCADE,
    recipe_id        UUID NULL REFERENCES work_recipes(id) ON DELETE SET NULL,
    recipe_name      TEXT NOT NULL DEFAULT '',
    domain           TEXT NOT NULL DEFAULT '',
    recipe_version   INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'running',
    inputs           JSONB NOT NULL DEFAULT '{}'::jsonb,
    llm_calls        INTEGER NOT NULL DEFAULT 0,
    error            TEXT NOT NULL DEFAULT '',
    failed_step_seq  INTEGER NULL,
    blocked_step_seq INTEGER NULL,
    blocked_risk     TEXT NOT NULL DEFAULT '',
    duration_ms      INTEGER NOT NULL DEFAULT 0,
    triggered_by     TEXT NOT NULL DEFAULT '',
    task_id          TEXT NULL,
    started_at       TIMESTAMPTZ NULL,
    finished_at      TIMESTAMPTZ NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT recipe_runs_status_valid CHECK (
        status IN ('running', 'success', 'failed', 'blocked', 'cancelled')
    ),
    CONSTRAINT recipe_runs_llm_calls_nonneg CHECK (llm_calls >= 0)
);

CREATE INDEX IF NOT EXISTS idx_recipe_runs_recipe
    ON recipe_runs (recipe_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_recipe_runs_lookup
    ON recipe_runs (domain, recipe_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_recipe_runs_status
    ON recipe_runs (status, created_at DESC);

-- -------------------------------------------------------- recipe_run_steps
CREATE TABLE IF NOT EXISTS recipe_run_steps (
    id           BIGSERIAL PRIMARY KEY,
    run_id       UUID NOT NULL REFERENCES recipe_runs(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    phase        TEXT NOT NULL DEFAULT 'step',
    action       TEXT NOT NULL,
    risk         TEXT NOT NULL DEFAULT 'READ',
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    output       JSONB NULL,
    llm_calls    INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT recipe_run_steps_action_valid CHECK (
        action IN ('navigate', 'click', 'fill', 'select', 'press',
                   'upload', 'download', 'snapshot', 'api_call')
    ),
    CONSTRAINT recipe_run_steps_risk_valid CHECK (
        risk IN ('READ', 'WRITE_INTERNAL', 'WRITE_EXTERNAL', 'IRREVERSIBLE')
    ),
    CONSTRAINT recipe_run_steps_status_valid CHECK (
        status IN ('success', 'failed', 'blocked', 'skipped')
    )
);

CREATE INDEX IF NOT EXISTS idx_recipe_run_steps_run
    ON recipe_run_steps (run_id, seq);

-- append-only. 실행 이력은 사후에 고칠 수 있으면 증거가 아니다.
-- 삭제는 recipe_runs 의 ON DELETE CASCADE 경로만 허용한다.
CREATE OR REPLACE FUNCTION recipe_run_steps_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'recipe_run_steps 는 append-only 입니다 (% 시도됨). 실행 이력은 수정/삭제할 수 없습니다.',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_recipe_run_steps_no_update ON recipe_run_steps;
CREATE TRIGGER trg_recipe_run_steps_no_update
    BEFORE UPDATE ON recipe_run_steps
    FOR EACH ROW EXECUTE FUNCTION recipe_run_steps_append_only();

DROP TRIGGER IF EXISTS trg_recipe_run_steps_no_delete ON recipe_run_steps;
CREATE TRIGGER trg_recipe_run_steps_no_delete
    BEFORE DELETE ON recipe_run_steps
    FOR EACH ROW
    WHEN (pg_trigger_depth() = 0)
    EXECUTE FUNCTION recipe_run_steps_append_only();

-- -------------------------------------------------------- recipe_approvals
CREATE TABLE IF NOT EXISTS recipe_approvals (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NULL REFERENCES tenants(id) ON DELETE CASCADE,
    run_id         UUID NOT NULL REFERENCES recipe_runs(id) ON DELETE CASCADE,
    step_seq       INTEGER NOT NULL,
    action         TEXT NOT NULL DEFAULT '',
    risk           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    summary        TEXT NOT NULL DEFAULT '',
    requested_by   TEXT NOT NULL DEFAULT '',
    decided_by     TEXT NOT NULL DEFAULT '',
    decision_note  TEXT NOT NULL DEFAULT '',
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at     TIMESTAMPTZ NULL,
    expires_at     TIMESTAMPTZ NULL,
    CONSTRAINT recipe_approvals_risk_valid CHECK (
        risk IN ('READ', 'WRITE_INTERNAL', 'WRITE_EXTERNAL', 'IRREVERSIBLE')
    ),
    CONSTRAINT recipe_approvals_status_valid CHECK (
        status IN ('pending', 'approved', 'rejected', 'expired')
    ),
    CONSTRAINT uq_recipe_approvals_step UNIQUE (run_id, step_seq)
);

CREATE INDEX IF NOT EXISTS idx_recipe_approvals_pending
    ON recipe_approvals (status, requested_at DESC)
    WHERE status = 'pending';

COMMIT;
