-- 매장비서 판매채널 광고/프로모션 원장 추가.
-- 기존 원장은 건드리지 않고, 테이블이 없을 때만 생성한다.

CREATE TABLE IF NOT EXISTS yeoljeong_delivery_ads (
    row_id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_yf_delivery_ads_business_branch
    ON yeoljeong_delivery_ads (business_id, branch);
