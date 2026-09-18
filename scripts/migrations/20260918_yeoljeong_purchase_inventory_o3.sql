-- FOOD-O3-PURCHASE-INVENTORY-20260918
-- 오비서 O3 매입·재고: 기존 재고/발주 테이블에 회사(company_id) 귀속을 더하고
-- 재고 실사 스냅샷 테이블을 신설한다.
--
-- 기존 테이블(yeoljeong_inventory_items / yeoljeong_purchase_orders /
-- yeoljeong_stock_movements)은 scripts/migrations/yeoljeong_inventory_tables.sql
-- 에서 만들어진다. 여기서는 확장만 하며 재실행해도 안전하다.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) 재고 품목: 회사 범위
-- ---------------------------------------------------------------------------
ALTER TABLE yeoljeong_inventory_items
    ADD COLUMN IF NOT EXISTS company_id TEXT NOT NULL DEFAULT '';

UPDATE yeoljeong_inventory_items
   SET company_id = business_id
 WHERE company_id = '' AND business_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_yeoljeong_inventory_items_company
    ON yeoljeong_inventory_items (company_id);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_inventory_items_company_name
    ON yeoljeong_inventory_items (company_id, name);

-- ---------------------------------------------------------------------------
-- 2) 발주: 회사 범위 + 입고 검증 기록
-- ---------------------------------------------------------------------------
ALTER TABLE yeoljeong_purchase_orders
    ADD COLUMN IF NOT EXISTS company_id TEXT NOT NULL DEFAULT '';
ALTER TABLE yeoljeong_purchase_orders
    ADD COLUMN IF NOT EXISTS received_by TEXT DEFAULT '';
ALTER TABLE yeoljeong_purchase_orders
    ADD COLUMN IF NOT EXISTS received_items JSONB DEFAULT '[]';

UPDATE yeoljeong_purchase_orders
   SET company_id = business_id
 WHERE company_id = '' AND business_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_yeoljeong_purchase_orders_company
    ON yeoljeong_purchase_orders (company_id);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_purchase_orders_company_date
    ON yeoljeong_purchase_orders (company_id, order_date DESC);

-- ---------------------------------------------------------------------------
-- 3) 수불: 회사 범위 + 실사 조정 수불(실행자)
-- ---------------------------------------------------------------------------
ALTER TABLE yeoljeong_stock_movements
    ADD COLUMN IF NOT EXISTS company_id TEXT NOT NULL DEFAULT '';
ALTER TABLE yeoljeong_stock_movements
    ADD COLUMN IF NOT EXISTS created_by TEXT DEFAULT '';

UPDATE yeoljeong_stock_movements
   SET company_id = business_id
 WHERE company_id = '' AND business_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_yeoljeong_stock_movements_company
    ON yeoljeong_stock_movements (company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_stock_movements_company_item
    ON yeoljeong_stock_movements (company_id, item_id);

-- ---------------------------------------------------------------------------
-- 4) 재고 실사 스냅샷 (신규)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS yeoljeong_stock_balances (
    id TEXT PRIMARY KEY DEFAULT 'sb-' || substr(md5(random()::text), 1, 12),
    company_id TEXT NOT NULL,
    business_id TEXT DEFAULT '',
    branch_id TEXT DEFAULT '',
    item_id TEXT NOT NULL,
    item_name TEXT DEFAULT '',
    unit TEXT DEFAULT 'ea',
    system_quantity NUMERIC NOT NULL DEFAULT 0,
    counted_quantity NUMERIC NOT NULL DEFAULT 0,
    difference NUMERIC NOT NULL DEFAULT 0,
    counted_by TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    counted_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yeoljeong_stock_balances_company
    ON yeoljeong_stock_balances (company_id, counted_at DESC);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_stock_balances_item
    ON yeoljeong_stock_balances (company_id, item_id, counted_at DESC);

COMMIT;
