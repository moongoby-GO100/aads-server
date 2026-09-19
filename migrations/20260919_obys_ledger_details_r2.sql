-- Tenant-scoped manual sales/purchase ledger and dedicated card ledger.
-- Additive only: uploaded source rows remain immutable in their existing table.
BEGIN;

CREATE TABLE IF NOT EXISTS yeoljeong_manual_ledger_entries (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    category TEXT NOT NULL CHECK (category IN ('sales','purchase')),
    occurred_on DATE NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    supply_amount NUMERIC(18,2) NOT NULL CHECK (supply_amount >= 0),
    tax_amount NUMERIC(18,2) NOT NULL CHECK (tax_amount >= 0),
    total_amount NUMERIC(18,2) NOT NULL CHECK (total_amount >= 0 AND total_amount = supply_amount + tax_amount),
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source = 'manual'),
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_manual_ledger_scope_date
    ON yeoljeong_manual_ledger_entries (tenant_id,business_id,category,occurred_on DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS yeoljeong_card_transactions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL,
    merchant TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    supply_amount NUMERIC(18,2) NOT NULL CHECK (supply_amount >= 0),
    tax_amount NUMERIC(18,2) NOT NULL CHECK (tax_amount >= 0),
    total_amount NUMERIC(18,2) NOT NULL CHECK (total_amount >= 0 AND total_amount = supply_amount + tax_amount),
    card_last4 TEXT NOT NULL CHECK (card_last4 ~ '^[0-9]{4}$'),
    source TEXT NOT NULL DEFAULT 'manual',
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_card_scope_date
    ON yeoljeong_card_transactions (tenant_id,business_id,occurred_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS yeoljeong_card_uploads (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size BIGINT NOT NULL CHECK (byte_size BETWEEN 1 AND 10485760),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review','imported','duplicate','rejected')),
    imported_rows INTEGER NOT NULL DEFAULT 0,
    rejected_rows INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_yeoljeong_card_uploads_active_digest
    ON yeoljeong_card_uploads (tenant_id,business_id,sha256)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_yeoljeong_card_uploads_scope_created
    ON yeoljeong_card_uploads (tenant_id,business_id,created_at DESC);

COMMIT;
