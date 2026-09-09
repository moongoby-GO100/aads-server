CREATE TABLE IF NOT EXISTS yeoljeong_inventory_items (
    id TEXT PRIMARY KEY DEFAULT 'inv-' || substr(md5(random()::text), 1, 12),
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    branch_id TEXT DEFAULT '',
    name TEXT NOT NULL,
    category TEXT DEFAULT '',
    unit TEXT DEFAULT 'ea',
    current_stock NUMERIC DEFAULT 0,
    min_stock NUMERIC DEFAULT 0,
    unit_cost NUMERIC DEFAULT 0,
    supplier TEXT DEFAULT '',
    supplier_code TEXT DEFAULT '',
    last_ordered_at TIMESTAMPTZ,
    memo TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS yeoljeong_purchase_orders (
    id TEXT PRIMARY KEY DEFAULT 'po-' || substr(md5(random()::text), 1, 12),
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    branch_id TEXT DEFAULT '',
    order_date DATE NOT NULL DEFAULT CURRENT_DATE,
    supplier TEXT DEFAULT '',
    supplier_type TEXT DEFAULT 'marketbom',
    status TEXT DEFAULT 'draft',
    total_amount NUMERIC DEFAULT 0,
    items JSONB DEFAULT '[]',
    received_at TIMESTAMPTZ,
    invoice_number TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS yeoljeong_stock_movements (
    id TEXT PRIMARY KEY DEFAULT 'sm-' || substr(md5(random()::text), 1, 12),
    item_id TEXT NOT NULL,
    business_id TEXT NOT NULL DEFAULT 'biz-mia',
    movement_type TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    reference_id TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
