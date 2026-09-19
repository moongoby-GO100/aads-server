-- O11: explicit business ownership and tenant-scoped durable ledger uploads.
BEGIN;

ALTER TABLE IF EXISTS yeoljeong_businesses ADD COLUMN IF NOT EXISTS tenant_id UUID;
CREATE INDEX IF NOT EXISTS idx_yeoljeong_businesses_tenant ON yeoljeong_businesses (tenant_id, id);

CREATE TABLE IF NOT EXISTS yeoljeong_business_tenant_mapping (
    business_id TEXT PRIMARY KEY REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    tenant_id UUID NOT NULL,
    evidence TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Read-only inventory evidence recorded in the 2026-09-19 remediation brief.
-- AADS Internal is intentionally absent and receives no legacy ownership.
INSERT INTO yeoljeong_business_tenant_mapping (business_id, tenant_id, evidence)
VALUES
 ('biz-eonni-naengmyeon', '15055cac-71b0-45ec-b714-7093dde189ff', 'OBYS DB inventory 2026-09-19: existing Yeoljeong business biz-eonni-naengmyeon; remediation directive tenant-32'),
 ('biz-junghwa', '15055cac-71b0-45ec-b714-7093dde189ff', 'OBYS DB inventory 2026-09-19: existing Yeoljeong business biz-junghwa; remediation directive tenant-32'),
 ('biz-mia', '15055cac-71b0-45ec-b714-7093dde189ff', 'OBYS DB inventory 2026-09-19: existing Yeoljeong business biz-mia; remediation directive tenant-32'),
 ('biz-sungshin', '15055cac-71b0-45ec-b714-7093dde189ff', 'OBYS DB inventory 2026-09-19: existing Yeoljeong business biz-sungshin; remediation directive tenant-32')
ON CONFLICT (business_id) DO UPDATE
SET tenant_id = EXCLUDED.tenant_id, evidence = EXCLUDED.evidence;

UPDATE yeoljeong_businesses AS business
SET tenant_id = mapping.tenant_id, updated_at = NOW(), updated_by = 'migration_20260919_o11'
FROM yeoljeong_business_tenant_mapping AS mapping
WHERE business.id = mapping.business_id
  AND business.tenant_id IS DISTINCT FROM mapping.tenant_id;

CREATE TABLE IF NOT EXISTS yeoljeong_uploads (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    category TEXT NOT NULL CHECK (category IN ('sales','purchase','transaction')),
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size BIGINT NOT NULL CHECK (byte_size BETWEEN 1 AND 10485760),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('imported','duplicate','rejected','pending_review')),
    imported_rows INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0,
    rejected_rows INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
-- Active duplicates are idempotent. A soft-deleted file may be uploaded again.
CREATE UNIQUE INDEX IF NOT EXISTS uq_yeoljeong_uploads_active_digest
    ON yeoljeong_uploads (tenant_id,business_id,category,sha256) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_yeoljeong_uploads_scope_created
    ON yeoljeong_uploads (tenant_id,business_id,category,created_at DESC);

CREATE TABLE IF NOT EXISTS yeoljeong_uploaded_ledger_rows (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    upload_id UUID NOT NULL REFERENCES yeoljeong_uploads(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK (category IN ('sales','purchase','transaction')),
    source_hash TEXT NOT NULL,
    occurred_on DATE,
    counterparty TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    amount NUMERIC(18,2) NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id,business_id,category,source_hash)
);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_uploaded_ledger_scope_date
    ON yeoljeong_uploaded_ledger_rows (tenant_id,business_id,category,occurred_on DESC);

COMMIT;
