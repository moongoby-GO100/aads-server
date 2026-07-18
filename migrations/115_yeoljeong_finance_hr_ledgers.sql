-- 매장비서 HR/계약/급여 JSON 원장 PostgreSQL 전환 준비 스키마.
-- 운영 적용 전 원장 백업과 API 호환 검증이 필요하다.
-- 파괴적 명령은 포함하지 않는다.

CREATE TABLE IF NOT EXISTS yeoljeong_employee_join_requests (
    id TEXT PRIMARY KEY,
    employee_email TEXT NOT NULL,
    employee_email_masked TEXT NOT NULL DEFAULT '',
    employee_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'employee',
    status TEXT NOT NULL DEFAULT 'pending',
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_memo TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_yf_join_requests_email
    ON yeoljeong_employee_join_requests (employee_email);
CREATE INDEX IF NOT EXISTS idx_yf_join_requests_status
    ON yeoljeong_employee_join_requests (status);
CREATE INDEX IF NOT EXISTS idx_yf_join_requests_business_branch
    ON yeoljeong_employee_join_requests (business_id, branch);

CREATE TABLE IF NOT EXISTS yeoljeong_onboarding_documents (
    id TEXT PRIMARY KEY,
    employee_request_id TEXT NOT NULL DEFAULT '',
    employee_email TEXT NOT NULL,
    employee_email_masked TEXT NOT NULL DEFAULT '',
    employee_name TEXT NOT NULL DEFAULT '',
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL,
    document_label TEXT NOT NULL DEFAULT '',
    requirement TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'uploaded',
    original_filename TEXT NOT NULL DEFAULT '',
    stored_filename TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    size_bytes BIGINT NOT NULL DEFAULT 0,
    issue_date TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    review_memo TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_yf_onboarding_email
    ON yeoljeong_onboarding_documents (employee_email);
CREATE INDEX IF NOT EXISTS idx_yf_onboarding_type_status
    ON yeoljeong_onboarding_documents (document_type, status);

CREATE TABLE IF NOT EXISTS yeoljeong_contracts (
    id TEXT PRIMARY KEY,
    employee_email TEXT NOT NULL,
    employee_email_masked TEXT NOT NULL DEFAULT '',
    employee_name TEXT NOT NULL DEFAULT '',
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    contract_type TEXT NOT NULL DEFAULT 'part_time',
    document_kind TEXT NOT NULL DEFAULT 'standard_employment_contract',
    template_version TEXT NOT NULL DEFAULT '',
    print_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    sign_token_hash TEXT NOT NULL DEFAULT '',
    requested_at TIMESTAMPTZ,
    signed_at TIMESTAMPTZ,
    created_by TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    signer_email TEXT NOT NULL DEFAULT '',
    signer_name TEXT NOT NULL DEFAULT '',
    contract_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_yf_contracts_email
    ON yeoljeong_contracts (employee_email);
CREATE INDEX IF NOT EXISTS idx_yf_contracts_status
    ON yeoljeong_contracts (status);
CREATE INDEX IF NOT EXISTS idx_yf_contracts_business_branch
    ON yeoljeong_contracts (business_id, branch);

CREATE TABLE IF NOT EXISTS yeoljeong_payroll_statements (
    id TEXT PRIMARY KEY,
    employee_email TEXT NOT NULL,
    employee_email_masked TEXT NOT NULL DEFAULT '',
    employee_name TEXT NOT NULL DEFAULT '',
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    payroll_month TEXT NOT NULL DEFAULT '',
    gross_pay BIGINT NOT NULL DEFAULT 0,
    tax_withholding BIGINT NOT NULL DEFAULT 0,
    insurance_deduction BIGINT NOT NULL DEFAULT 0,
    other_deduction BIGINT NOT NULL DEFAULT 0,
    net_pay BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT NOT NULL DEFAULT '',
    confirmed_at TIMESTAMPTZ,
    statement_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_yf_payroll_email
    ON yeoljeong_payroll_statements (employee_email);
CREATE INDEX IF NOT EXISTS idx_yf_payroll_month
    ON yeoljeong_payroll_statements (payroll_month);
CREATE INDEX IF NOT EXISTS idx_yf_payroll_business_branch
    ON yeoljeong_payroll_statements (business_id, branch);
