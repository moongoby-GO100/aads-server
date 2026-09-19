-- Tenant-scoped manual bank ledger. Kept in its own additive migration so a
-- previously applied sales/card migration can never hide this table addition.
BEGIN;

CREATE TABLE IF NOT EXISTS yeoljeong_manual_bank_transactions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in','out')),
    amount BIGINT NOT NULL CHECK (amount >= 0),
    balance BIGINT CHECK (balance IS NULL OR balance >= 0),
    counterparty TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    account_label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source = 'manual'),
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_yeoljeong_manual_bank_scope_date
    ON yeoljeong_manual_bank_transactions (tenant_id,business_id,occurred_at DESC)
    WHERE deleted_at IS NULL;

COMMIT;
