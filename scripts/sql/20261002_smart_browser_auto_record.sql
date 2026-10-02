-- AADS-SMARTBROWSER-UNIFY-AUTORECORD-20261002
-- browser_* 반복 성공 경로의 자동 레시피 초안화 (SMART_BROWSER_AUTO_RECORD).
--
-- 적용은 CEO 승인 후 점검 창구에서. 이 파일은 생성까지만 한다.
--   * 추가 전용·멱등 (CREATE ... IF NOT EXISTS). DROP / TRUNCATE 없음.
--   * steps 에는 fill 값·query·쿠키·토큰이 들어가지 않는다 (auto_record.py 가 정규화한 단계만).

BEGIN;

CREATE TABLE IF NOT EXISTS smart_browser_auto_traces (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    domain          TEXT NOT NULL,
    signature       TEXT NOT NULL,
    chat_session_id TEXT NOT NULL,
    steps           JSONB NOT NULL,
    success_count   INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_smart_browser_auto_traces_session
        UNIQUE (tenant_id, domain, signature, chat_session_id)
);

CREATE INDEX IF NOT EXISTS idx_smart_browser_auto_traces_signature
    ON smart_browser_auto_traces (tenant_id, domain, signature);

-- (tenant, domain, signature) 당 한 행 — 초안 중복 생성을 막는 선점 장부.
CREATE TABLE IF NOT EXISTS smart_browser_auto_drafts (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    domain          TEXT NOT NULL,
    signature       TEXT NOT NULL,
    recipe_name     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'claimed',
    registration_id UUID NULL REFERENCES work_recipe_registration_requests(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, domain, signature),
    CONSTRAINT smart_browser_auto_drafts_status_valid
        CHECK (status IN ('claimed', 'drafted', 'skipped_existing'))
);

COMMIT;
