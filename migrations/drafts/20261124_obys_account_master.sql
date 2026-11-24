-- 계정과목 마스터 초안. ACCT/OBYS DB 전용, 자동 적용 대상 아님.
-- 검토 후 별도 승인으로 적용한다. 기존 전표와 계정 상수는 변경하지 않는다.
BEGIN;
CREATE TABLE IF NOT EXISTS obys_account_master (
    tenant_id uuid NOT NULL,
    account_code text NOT NULL CHECK (btrim(account_code) <> ''),
    account_name text NOT NULL CHECK (btrim(account_name) <> ''),
    source text NOT NULL,
    source_company_label text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, account_code)
);
COMMIT;
