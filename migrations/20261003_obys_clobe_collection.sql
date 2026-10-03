-- 오비서(OBYS DB) 클로브 다회사 읽기 전용 수집 계약. 롤백: migrations/rollback/20261003_obys_clobe_collection.down.sql
-- 적용 대상은 OBYS_DATABASE_URL (AADS DB 아님). 멱등: 여러 번 실행해도 같은 결과.
-- 원장 확정은 yeoljeong_journals(동결, 소유 ACCT)가 아니라 전용 obys_clobe_ledger_entry 로만 한다.
BEGIN;

-- 1) 클로브 회사 ↔ 오비서 사업자 연결. companyId 가 불변 키다. 이름만으로는 연결하지 않는다.
CREATE TABLE IF NOT EXISTS obys_clobe_company_link (
    clobe_company_id  TEXT PRIMARY KEY,
    company_name      TEXT NOT NULL DEFAULT '',
    reg_no            TEXT NOT NULL DEFAULT '',
    clobe_role        TEXT NOT NULL DEFAULT '',
    link_status       TEXT NOT NULL DEFAULT 'review'
        CHECK (link_status IN ('review', 'linked', 'blocked')),
    link_basis        TEXT NOT NULL DEFAULT '',
    tenant_id         UUID,
    business_id       TEXT,
    candidate_business_ids TEXT[] NOT NULL DEFAULT '{}',
    permission_ok     BOOLEAN,
    permission_checked_at TIMESTAMPTZ,
    permission_error  TEXT,
    approved_by       TEXT,
    approved_at       TIMESTAMPTZ,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    missing_streak    INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT obys_clobe_link_scope CHECK (
        (link_status = 'linked' AND tenant_id IS NOT NULL AND business_id IS NOT NULL)
        OR link_status <> 'linked'
    )
);
ALTER TABLE obys_clobe_company_link ADD COLUMN IF NOT EXISTS missing_streak INTEGER NOT NULL DEFAULT 0;
-- 하나의 사업자에 클로브 회사 둘이 붙는 것을 막는다(열정국밥 ≠ 다른 회사).
CREATE UNIQUE INDEX IF NOT EXISTS obys_clobe_link_business_uq
    ON obys_clobe_company_link (tenant_id, business_id) WHERE link_status = 'linked';

-- 2) 회사별 수집 임대(lease). 쓰기는 이 행을 FOR UPDATE 로 잠그고 epoch 를 대조한 뒤에만 한다.
CREATE TABLE IF NOT EXISTS obys_clobe_collection_lease (
    clobe_company_id TEXT PRIMARY KEY,
    owner_instance   TEXT NOT NULL,
    owner_epoch      BIGINT NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL
);

-- 3) 수집 실행 이력. resume_state 는 부분 수집을 이어받는 체크포인트다.
CREATE TABLE IF NOT EXISTS obys_clobe_collection_run (
    run_id           UUID PRIMARY KEY,
    clobe_company_id TEXT NOT NULL,
    tenant_id        UUID NOT NULL,
    business_id      TEXT NOT NULL,
    data_kind        TEXT NOT NULL,
    mode             TEXT NOT NULL DEFAULT 'live' CHECK (mode IN ('live', 'shadow')),
    period_start     DATE NOT NULL,
    period_end       DATE NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'lease_lost')),
    owner_instance   TEXT NOT NULL,
    owner_epoch      BIGINT NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1,
    chain_id         UUID NOT NULL,
    resume_of        UUID,
    resume_state     JSONB NOT NULL DEFAULT '{}'::jsonb,
    counts           JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_as_of     TIMESTAMPTZ,
    error_code       TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS obys_clobe_run_chain_idx ON obys_clobe_collection_run (chain_id);
CREATE INDEX IF NOT EXISTS obys_clobe_run_company_idx
    ON obys_clobe_collection_run (clobe_company_id, data_kind, started_at DESC);

-- 4) 원본 항목(불변, hash 로 버전). stage 가 검토함 → 회계검토 → 확정의 분리선이다.
CREATE TABLE IF NOT EXISTS obys_clobe_item (
    item_id          UUID PRIMARY KEY,
    tenant_id        UUID NOT NULL,
    business_id      TEXT NOT NULL,
    clobe_company_id TEXT NOT NULL,
    data_kind        TEXT NOT NULL,
    source_key       TEXT NOT NULL,
    item_hash        TEXT NOT NULL,
    institution      TEXT NOT NULL DEFAULT '',
    occurred_on      DATE,
    amount           NUMERIC(18,2),
    direction        TEXT NOT NULL DEFAULT '',
    counterparty     TEXT NOT NULL DEFAULT '',
    source_as_of     TIMESTAMPTZ,
    payload          JSONB NOT NULL,
    stage            TEXT NOT NULL DEFAULT 'review_box'
        CHECK (stage IN ('review_box', 'reviewed', 'confirmed', 'rejected', 'superseded')),
    supersedes_item_id UUID,
    first_run_id     UUID NOT NULL,
    last_run_id      UUID NOT NULL,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by      TEXT,
    reviewed_at      TIMESTAMPTZ,
    confirmed_by     TEXT,
    confirmed_at     TIMESTAMPTZ,
    CONSTRAINT obys_clobe_item_version_uq
        UNIQUE (tenant_id, business_id, data_kind, source_key, item_hash)
);
CREATE INDEX IF NOT EXISTS obys_clobe_item_stage_idx
    ON obys_clobe_item (tenant_id, business_id, stage, data_kind, occurred_on DESC);
CREATE INDEX IF NOT EXISTS obys_clobe_item_key_idx
    ON obys_clobe_item (tenant_id, business_id, data_kind, source_key);

-- 5) 확정 원장. (사업자, 종류, source_key) 가 유일 — 같은 배치를 다시 돌려도 행이 늘지 않는다.
CREATE TABLE IF NOT EXISTS obys_clobe_ledger_entry (
    entry_id     UUID PRIMARY KEY,
    tenant_id    UUID NOT NULL,
    business_id  TEXT NOT NULL,
    data_kind    TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    item_id      UUID NOT NULL REFERENCES obys_clobe_item (item_id),
    item_hash    TEXT NOT NULL,
    occurred_on  DATE,
    amount       NUMERIC(18,2),
    direction    TEXT NOT NULL DEFAULT '',
    revision     INTEGER NOT NULL DEFAULT 1,
    confirmed_by TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT obys_clobe_ledger_identity_uq UNIQUE (tenant_id, business_id, data_kind, source_key)
);

-- 6) 회사·종류별 현재 상태(UI 계약). 마지막 성공/실패, 오류, 재시도.
CREATE TABLE IF NOT EXISTS obys_clobe_collection_state (
    clobe_company_id TEXT NOT NULL,
    data_kind        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'never_run',
    last_run_id      UUID,
    last_success_at  TIMESTAMPTZ,
    last_success_run_id UUID,
    last_failure_at  TIMESTAMPTZ,
    last_error_code  TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_retry_at    TIMESTAMPTZ,
    covered_from     DATE,
    covered_to       DATE,
    source_as_of     TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clobe_company_id, data_kind)
);

COMMIT;
