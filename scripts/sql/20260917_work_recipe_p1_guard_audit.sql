-- AADS-SB-P1-GUARD-AUDIT-INJECTION-20260917
-- 스마트 브라우저 P1 — 승인 게이트 · 감사 기록 · 크리덴셜 사용 로그 (FR-4~7).
--
-- 적용은 CEO 승인 후 점검 창구에서. 이 파일은 생성까지만 한다.
--
-- 성질
--   * 추가 전용(additive only). DROP / TRUNCATE / ALTER ... DROP 이 없다.
--   * 멱등. 두 번 돌려도 같은 상태가 된다 (IF NOT EXISTS / CREATE OR REPLACE).
--   * P0 파일(20260917_work_recipe_schema.sql)을 고치지 않는다. P0 가 이미 만든
--     것은 건너뛰고, 모자란 것만 채운다.
--
-- 이름 매핑 (PRD 5절 ↔ 실제 컬럼)
--   PRD 의 `risk_level` 은 P0 가 이미 만든 `risk` 컬럼이다. 같은 뜻의 컬럼을
--   하나 더 만들면 둘 중 하나가 반드시 낡고, 낡은 쪽을 읽는 코드가 사고를 낸다
--   (CLAUDE.md R-RELEASE 와 같은 이유). 그래서 컬럼은 하나로 두고 서비스 계층
--   (approval.py / audit.py)이 `risk_level` 이라는 이름으로 읽는다.
--
-- PostgreSQL 14+ 가 필요하다 (`CREATE OR REPLACE TRIGGER`). 운영은 15 다.

BEGIN;

-- ------------------------------------------------------- recipe_approvals
-- P0 에서 이미 만들어져 있다. 단독 적용도 되도록 전체 정의를 함께 둔다.
CREATE TABLE IF NOT EXISTS recipe_approvals (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NULL REFERENCES tenants(id) ON DELETE CASCADE,
    run_id         UUID NOT NULL REFERENCES recipe_runs(id) ON DELETE CASCADE,
    step_seq       INTEGER NOT NULL,
    action         TEXT NOT NULL DEFAULT '',
    risk           TEXT NOT NULL,              -- PRD 5절의 risk_level
    status         TEXT NOT NULL DEFAULT 'pending',
    summary        TEXT NOT NULL DEFAULT '',
    confirm_text   TEXT NOT NULL DEFAULT '',   -- IRREVERSIBLE 재확인 문구
    decision       TEXT NOT NULL DEFAULT '',   -- approve | reject | ''(미결정)
    reason         TEXT NOT NULL DEFAULT '',
    requested_by   TEXT NOT NULL DEFAULT '',
    decided_by     TEXT NOT NULL DEFAULT '',
    decision_note  TEXT NOT NULL DEFAULT '',   -- P0 컬럼. reason 과 같은 값을 쓴다.
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

-- P0 로 이미 만들어진 테이블에 P1 컬럼을 채운다.
ALTER TABLE recipe_approvals ADD COLUMN IF NOT EXISTS confirm_text  TEXT NOT NULL DEFAULT '';
ALTER TABLE recipe_approvals ADD COLUMN IF NOT EXISTS decision      TEXT NOT NULL DEFAULT '';
ALTER TABLE recipe_approvals ADD COLUMN IF NOT EXISTS reason        TEXT NOT NULL DEFAULT '';

-- decision 값 제약. 이미 있으면 그대로 둔다(ADD CONSTRAINT 에는 IF NOT EXISTS 가 없다).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'recipe_approvals_decision_valid'
    ) THEN
        ALTER TABLE recipe_approvals
            ADD CONSTRAINT recipe_approvals_decision_valid
            CHECK (decision IN ('', 'approve', 'reject'));
    END IF;
END
$$;

-- IRREVERSIBLE 승인은 재확인 문구 없이 통과할 수 없다 (PRD 4절).
-- 문서 규칙이 아니라 DB 제약으로 둔다 — 두 번 어긴 규칙은 코드로 옮긴다(R-ERRBOOK 3).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'recipe_approvals_confirm_required'
    ) THEN
        ALTER TABLE recipe_approvals
            ADD CONSTRAINT recipe_approvals_confirm_required
            CHECK (
                status <> 'approved'
                OR risk <> 'IRREVERSIBLE'
                OR length(btrim(confirm_text)) > 0
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_recipe_approvals_run
    ON recipe_approvals (run_id, step_seq);

CREATE INDEX IF NOT EXISTS idx_recipe_approvals_pending
    ON recipe_approvals (status, requested_at DESC)
    WHERE status = 'pending';

-- -------------------------------------------------------- recipe_run_steps
-- PRD 5절이 요구하는 감사 필드. 값이 없던 과거 행은 빈 문자열/NULL 로 남는다.
ALTER TABLE recipe_run_steps ADD COLUMN IF NOT EXISTS url             TEXT NOT NULL DEFAULT '';
ALTER TABLE recipe_run_steps ADD COLUMN IF NOT EXISTS selector_hash   TEXT NOT NULL DEFAULT '';
ALTER TABLE recipe_run_steps ADD COLUMN IF NOT EXISTS screenshot_path TEXT NOT NULL DEFAULT '';
ALTER TABLE recipe_run_steps ADD COLUMN IF NOT EXISTS approval_id     UUID NULL
    REFERENCES recipe_approvals(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_recipe_run_steps_approval
    ON recipe_run_steps (approval_id)
    WHERE approval_id IS NOT NULL;

-- --------------------------------------------------- recipe_credential_uses
-- FR-7: vault 사용 1회 = 감사 1행. 값은 남기지 않는다 — 어느 런이 어느 도메인의
-- 어느 항목을 썼는지까지만 남긴다(R-KEY). 이 행이 없으면 "도메인×레시피 범위
-- 제한"은 검증할 수 없는 규칙으로 남는다.
CREATE TABLE IF NOT EXISTS recipe_credential_uses (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES recipe_runs(id) ON DELETE CASCADE,
    domain          TEXT NOT NULL DEFAULT '',
    recipe_name     TEXT NOT NULL DEFAULT '',
    credential_ref  TEXT NOT NULL DEFAULT '',
    used_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recipe_credential_uses_run
    ON recipe_credential_uses (run_id, used_at DESC);

CREATE INDEX IF NOT EXISTS idx_recipe_credential_uses_domain
    ON recipe_credential_uses (domain, used_at DESC);

-- --------------------------------------------------------- append-only 강제
-- AC-5. 실행 이력은 사후에 고칠 수 있으면 증거가 아니다.
-- 삭제는 recipe_runs 의 ON DELETE CASCADE 경로(pg_trigger_depth() > 0)만 허용한다.
CREATE OR REPLACE FUNCTION recipe_run_steps_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'recipe_run_steps 는 append-only 입니다 (% 시도됨). 실행 이력은 수정/삭제할 수 없습니다.',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_recipe_run_steps_no_update
    BEFORE UPDATE ON recipe_run_steps
    FOR EACH ROW EXECUTE FUNCTION recipe_run_steps_append_only();

CREATE OR REPLACE TRIGGER trg_recipe_run_steps_no_delete
    BEFORE DELETE ON recipe_run_steps
    FOR EACH ROW
    WHEN (pg_trigger_depth() = 0)
    EXECUTE FUNCTION recipe_run_steps_append_only();

CREATE OR REPLACE FUNCTION recipe_credential_uses_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'recipe_credential_uses 는 append-only 입니다 (% 시도됨).',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_recipe_credential_uses_no_update
    BEFORE UPDATE ON recipe_credential_uses
    FOR EACH ROW EXECUTE FUNCTION recipe_credential_uses_append_only();

CREATE OR REPLACE TRIGGER trg_recipe_credential_uses_no_delete
    BEFORE DELETE ON recipe_credential_uses
    FOR EACH ROW
    WHEN (pg_trigger_depth() = 0)
    EXECUTE FUNCTION recipe_credential_uses_append_only();

COMMIT;
