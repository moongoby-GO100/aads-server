-- OBYS tenant/business-scoped upload and canonical journal workflow (additive/idempotent).
BEGIN;

ALTER TABLE yeoljeong_uploads DROP CONSTRAINT IF EXISTS yeoljeong_uploads_category_check;
ALTER TABLE yeoljeong_uploads ADD CONSTRAINT yeoljeong_uploads_category_check
    CHECK (category IN ('sales','purchase','transaction','card')) NOT VALID;
ALTER TABLE yeoljeong_uploads VALIDATE CONSTRAINT yeoljeong_uploads_category_check;
ALTER TABLE yeoljeong_uploaded_ledger_rows DROP CONSTRAINT IF EXISTS yeoljeong_uploaded_ledger_rows_category_check;
ALTER TABLE yeoljeong_uploaded_ledger_rows ADD CONSTRAINT yeoljeong_uploaded_ledger_rows_category_check
    CHECK (category IN ('sales','purchase','transaction','card')) NOT VALID;
ALTER TABLE yeoljeong_uploaded_ledger_rows VALIDATE CONSTRAINT yeoljeong_uploaded_ledger_rows_category_check;

CREATE TABLE IF NOT EXISTS yeoljeong_journal_vouchers (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    voucher_no TEXT NOT NULL,
    transaction_date DATE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','needs_review','approved','posted','reversed')),
    source_type TEXT NOT NULL
        CHECK (source_type IN ('uploaded_ledger_row','manual_ledger_entry','card_transaction','bank_transaction')),
    source_id UUID NOT NULL,
    supply_amount NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (supply_amount >= 0),
    tax_amount NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    total_amount NUMERIC(18,2) NOT NULL CHECK (total_amount >= 0 AND total_amount = supply_amount + tax_amount),
    evidence_source TEXT NOT NULL DEFAULT '',
    export_status TEXT NOT NULL DEFAULT 'not_exported'
        CHECK (export_status IN ('not_exported','ready','exported','failed')),
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    posted_at TIMESTAMPTZ,
    reversed_at TIMESTAMPTZ,
    reversal_of_id UUID REFERENCES yeoljeong_journal_vouchers(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id,business_id,voucher_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_yeoljeong_journal_source
    ON yeoljeong_journal_vouchers (tenant_id,business_id,source_type,source_id)
    WHERE reversal_of_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_yeoljeong_journal_scope_status
    ON yeoljeong_journal_vouchers (tenant_id,business_id,status,transaction_date DESC);

CREATE TABLE IF NOT EXISTS yeoljeong_journal_lines (
    id UUID PRIMARY KEY,
    voucher_id UUID NOT NULL REFERENCES yeoljeong_journal_vouchers(id) ON DELETE CASCADE,
    line_no SMALLINT NOT NULL CHECK (line_no > 0),
    side TEXT NOT NULL CHECK (side IN ('debit','credit')),
    account_code TEXT NOT NULL,
    account_name TEXT NOT NULL,
    amount NUMERIC(18,2) NOT NULL CHECK (amount > 0),
    tax_code TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    UNIQUE (voucher_id,line_no)
);

COMMIT;

