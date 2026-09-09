CREATE TABLE IF NOT EXISTS yeoljeong_accounting_entries (
    id TEXT PRIMARY KEY DEFAULT 'acc-' || substr(md5(random()::text), 1, 12),
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    entry_date DATE NOT NULL,
    entry_type TEXT NOT NULL,
    category TEXT DEFAULT '',
    subcategory TEXT DEFAULT '',
    description TEXT DEFAULT '',
    debit_amount NUMERIC DEFAULT 0,
    credit_amount NUMERIC DEFAULT 0,
    counterparty TEXT DEFAULT '',
    source_type TEXT DEFAULT 'manual',
    source_id TEXT DEFAULT '',
    tax_type TEXT DEFAULT 'vat',
    vat_amount NUMERIC DEFAULT 0,
    receipt_id TEXT DEFAULT '',
    verified BOOLEAN DEFAULT FALSE,
    verified_by TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS yeoljeong_tax_reports (
    id TEXT PRIMARY KEY DEFAULT 'tax-' || substr(md5(random()::text), 1, 12),
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    report_type TEXT NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    status TEXT DEFAULT 'draft',
    total_sales NUMERIC DEFAULT 0,
    total_purchases NUMERIC DEFAULT 0,
    vat_payable NUMERIC DEFAULT 0,
    tax_amount NUMERIC DEFAULT 0,
    details JSONB DEFAULT '{}',
    submitted_at TIMESTAMPTZ,
    memo TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS yeoljeong_category_rules (
    id TEXT PRIMARY KEY DEFAULT 'cr-' || substr(md5(random()::text), 1, 12),
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    keyword TEXT NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT DEFAULT '',
    tax_type TEXT DEFAULT 'vat',
    priority INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yeoljeong_accounting_entries_business_date ON yeoljeong_accounting_entries (business_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_accounting_entries_category ON yeoljeong_accounting_entries (business_id, category);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_accounting_entries_type ON yeoljeong_accounting_entries (business_id, entry_type);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_accounting_entries_source ON yeoljeong_accounting_entries (source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_tax_reports_business ON yeoljeong_tax_reports (business_id, report_type, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_tax_reports_status ON yeoljeong_tax_reports (business_id, status);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_category_rules_business ON yeoljeong_category_rules (business_id, priority DESC);
