-- 매장비서 배달앱 계정/매출/정산/리뷰 JSON 원장 PostgreSQL 전환 준비 스키마.
-- 파괴적 명령은 포함하지 않으며, API는 테이블이 없으면 기존 JSON 원장을 계속 사용한다.

CREATE TABLE IF NOT EXISTS yeoljeong_platform_accounts (
    row_id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS yeoljeong_delivery_sales (
    row_id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS yeoljeong_delivery_settlements (
    row_id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS yeoljeong_delivery_reviews (
    row_id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS yeoljeong_delivery_collection_status (
    row_id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_yf_platform_accounts_business_branch
    ON yeoljeong_platform_accounts (business_id, branch);
CREATE INDEX IF NOT EXISTS idx_yf_delivery_sales_business_branch
    ON yeoljeong_delivery_sales (business_id, branch);
CREATE INDEX IF NOT EXISTS idx_yf_delivery_settlements_business_branch
    ON yeoljeong_delivery_settlements (business_id, branch);
CREATE INDEX IF NOT EXISTS idx_yf_delivery_reviews_business_branch
    ON yeoljeong_delivery_reviews (business_id, branch);
CREATE INDEX IF NOT EXISTS idx_yf_delivery_collection_status_business_branch
    ON yeoljeong_delivery_collection_status (business_id, branch);

UPDATE yeoljeong_platform_accounts
   SET payload = payload - 'password'
 WHERE payload ? 'password';
