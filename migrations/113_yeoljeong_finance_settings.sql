-- 113: Yeoljeong store assistant finance/settings persistence.
-- Keep business and branch master data in PostgreSQL while preserving the
-- existing JSON file fallback for non-DB local runs.

BEGIN;

CREATE TABLE IF NOT EXISTS yeoljeong_businesses (
    id              TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL DEFAULT 'individual',
    name            TEXT NOT NULL,
    registration_no TEXT NOT NULL DEFAULT '',
    representative  TEXT NOT NULL DEFAULT '',
    tax_type        TEXT NOT NULL DEFAULT '',
    opened_at       TEXT NOT NULL DEFAULT '',
    address         TEXT NOT NULL DEFAULT '',
    memo            TEXT NOT NULL DEFAULT '',
    sort_order      INTEGER NOT NULL DEFAULT 0,
    updated_by      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS yeoljeong_branches (
    id          TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    phone       TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    updated_by  TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS yeoljeong_settings (
    scope      TEXT PRIMARY KEY,
    data       JSONB NOT NULL DEFAULT '{}',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yeoljeong_businesses_active
    ON yeoljeong_businesses(sort_order, id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_yeoljeong_branches_business_active
    ON yeoljeong_branches(business_id, sort_order, id)
    WHERE deleted_at IS NULL;

INSERT INTO yeoljeong_businesses
    (id, entity_type, name, registration_no, representative, tax_type, memo, sort_order, updated_by)
VALUES
    ('biz-junghwa', 'individual', '열정국밥 중화점', '기초등록 필요', '미등록', '일반과세', '개인사업자 1', 1, 'migration_113'),
    ('biz-sungshin', 'individual', '열정국밥 성신여대점', '기초등록 필요', '미등록', '일반과세', '개인사업자 2', 2, 'migration_113'),
    ('biz-mia', 'individual', '열정국밥_미아점', '기초등록 필요', '미등록', '일반과세', '개인사업자 3', 3, 'migration_113')
ON CONFLICT (id) DO UPDATE
   SET entity_type = EXCLUDED.entity_type,
       name = EXCLUDED.name,
       sort_order = EXCLUDED.sort_order,
       updated_by = EXCLUDED.updated_by,
       updated_at = NOW(),
       deleted_at = NULL;

-- Do not soft-delete unknown business rows here. Other sessions may add DB-side
-- businesses before the UI is ready to expose them; the app canonicalizes the
-- visible settings without destroying those rows.

INSERT INTO yeoljeong_branches
    (id, business_id, name, status, sort_order, updated_by)
VALUES
    ('branch-junghwa', 'biz-junghwa', '중화점', 'active', 1, 'migration_113'),
    ('branch-sungshin', 'biz-sungshin', '성신여대점', 'active', 2, 'migration_113'),
    ('branch-gangbuk-mia', 'biz-mia', '열정국밥_미아점', 'active', 3, 'migration_113')
ON CONFLICT (id) DO UPDATE
   SET business_id = EXCLUDED.business_id,
       name = EXCLUDED.name,
       status = EXCLUDED.status,
       sort_order = EXCLUDED.sort_order,
       updated_by = EXCLUDED.updated_by,
       updated_at = NOW(),
       deleted_at = NULL;

-- Same rule for branches: seed/update the canonical rows, but keep external
-- rows intact to avoid clobbering concurrent DB work.

INSERT INTO yeoljeong_settings (scope, data, updated_by)
VALUES (
    'ui',
    '{"accounts":[],"staff":[],"integrations":[]}'::jsonb,
    'migration_113'
)
ON CONFLICT (scope) DO NOTHING;

COMMIT;
