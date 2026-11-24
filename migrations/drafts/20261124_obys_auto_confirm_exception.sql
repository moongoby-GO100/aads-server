-- 클로브 자동 확정 이상 사유 초안 — 자동 적용 금지.
CREATE TABLE IF NOT EXISTS obys_auto_confirm_exception (
    item_id UUID PRIMARY KEY REFERENCES obys_clobe_item(item_id),
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL,
    reasons TEXT[] NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS obys_auto_confirm_exception_scope_idx
    ON obys_auto_confirm_exception(tenant_id, business_id, detected_at)
    WHERE resolved_at IS NULL;
