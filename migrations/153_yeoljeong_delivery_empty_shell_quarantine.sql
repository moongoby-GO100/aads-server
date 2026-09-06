-- Quarantine delivery ledger placeholder rows that were parsed from empty portal tables.
-- This is reversible: restore rows with
--   UPDATE <ledger_table> SET deleted_at = NULL
--    WHERE row_id IN (
--      SELECT row_id FROM yeoljeong_delivery_quality_quarantine
--       WHERE ledger_name = '<ledger_table>' AND reason = 'empty_shell_record'
--    );

CREATE TABLE IF NOT EXISTS yeoljeong_delivery_quality_quarantine (
    id TEXT PRIMARY KEY,
    ledger_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yf_delivery_quality_quarantine_ledger
    ON yeoljeong_delivery_quality_quarantine (ledger_name, reason, service);

WITH invalid_rows AS (
    SELECT
        'yeoljeong_delivery_sales' AS ledger_name,
        row_id,
        business_id,
        branch,
        payload->>'service' AS service,
        payload
    FROM yeoljeong_delivery_sales
    WHERE deleted_at IS NULL
      AND COALESCE(NULLIF(payload->>'gross_amount', '')::numeric, 0) = 0
),
inserted AS (
    INSERT INTO yeoljeong_delivery_quality_quarantine
        (id, ledger_name, row_id, business_id, branch, service, reason, payload)
    SELECT
        md5(ledger_name || ':' || row_id || ':empty_shell_record'),
        ledger_name,
        row_id,
        business_id,
        branch,
        service,
        'empty_shell_record',
        payload
    FROM invalid_rows
    ON CONFLICT (id) DO NOTHING
    RETURNING 1
)
UPDATE yeoljeong_delivery_sales target
   SET deleted_at = NOW(),
       updated_at = NOW(),
       payload = jsonb_set(target.payload, '{quality_status}', '"quarantined_empty_shell"', true)
  FROM invalid_rows
 WHERE target.row_id = invalid_rows.row_id;

WITH invalid_rows AS (
    SELECT
        'yeoljeong_delivery_settlements' AS ledger_name,
        row_id,
        business_id,
        branch,
        payload->>'service' AS service,
        payload
    FROM yeoljeong_delivery_settlements
    WHERE deleted_at IS NULL
      AND COALESCE(NULLIF(payload->>'settlement_amount', '')::numeric, 0) = 0
      AND COALESCE(NULLIF(payload->>'sales_amount', '')::numeric, 0) = 0
      AND COALESCE(NULLIF(payload->>'settlement_id', ''), '') = ''
),
inserted AS (
    INSERT INTO yeoljeong_delivery_quality_quarantine
        (id, ledger_name, row_id, business_id, branch, service, reason, payload)
    SELECT
        md5(ledger_name || ':' || row_id || ':empty_shell_record'),
        ledger_name,
        row_id,
        business_id,
        branch,
        service,
        'empty_shell_record',
        payload
    FROM invalid_rows
    ON CONFLICT (id) DO NOTHING
    RETURNING 1
)
UPDATE yeoljeong_delivery_settlements target
   SET deleted_at = NOW(),
       updated_at = NOW(),
       payload = jsonb_set(target.payload, '{quality_status}', '"quarantined_empty_shell"', true)
  FROM invalid_rows
 WHERE target.row_id = invalid_rows.row_id;

WITH invalid_rows AS (
    SELECT
        'yeoljeong_delivery_reviews' AS ledger_name,
        row_id,
        business_id,
        branch,
        payload->>'service' AS service,
        payload
    FROM yeoljeong_delivery_reviews
    WHERE deleted_at IS NULL
      AND COALESCE(NULLIF(payload->>'review_text', ''), '') = ''
      AND COALESCE(NULLIF(payload->>'rating', '')::numeric, 0) = 0
),
inserted AS (
    INSERT INTO yeoljeong_delivery_quality_quarantine
        (id, ledger_name, row_id, business_id, branch, service, reason, payload)
    SELECT
        md5(ledger_name || ':' || row_id || ':empty_shell_record'),
        ledger_name,
        row_id,
        business_id,
        branch,
        service,
        'empty_shell_record',
        payload
    FROM invalid_rows
    ON CONFLICT (id) DO NOTHING
    RETURNING 1
)
UPDATE yeoljeong_delivery_reviews target
   SET deleted_at = NOW(),
       updated_at = NOW(),
       payload = jsonb_set(target.payload, '{quality_status}', '"quarantined_empty_shell"', true)
  FROM invalid_rows
 WHERE target.row_id = invalid_rows.row_id;
