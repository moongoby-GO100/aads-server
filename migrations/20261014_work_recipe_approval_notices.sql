-- 자동화 승인 대기 알림 상태 (FR-18). Additive only.
-- work_recipe_registration_requests 는 건드리지 않고, 알림·재알림·만료 표시만 별도 메타로 둔다.
BEGIN;

CREATE TABLE IF NOT EXISTS work_recipe_approval_notices (
    registration_id  UUID PRIMARY KEY
                     REFERENCES work_recipe_registration_requests(id) ON DELETE CASCADE,
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    clock_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at      TIMESTAMPTZ NULL,
    reminded_at      TIMESTAMPTZ NULL,
    expired_at       TIMESTAMPTZ NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_work_recipe_approval_notices_tenant
    ON work_recipe_approval_notices (tenant_id);

COMMIT;
