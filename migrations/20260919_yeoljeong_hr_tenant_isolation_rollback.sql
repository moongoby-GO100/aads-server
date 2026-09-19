-- Schema rollback is non-destructive: HR rows and their attributed values remain.
BEGIN;
ALTER TABLE yeoljeong_employee_join_requests DROP CONSTRAINT IF EXISTS fk_yf_join_business_tenant;
ALTER TABLE yeoljeong_onboarding_documents DROP CONSTRAINT IF EXISTS fk_yf_documents_business_tenant;
ALTER TABLE yeoljeong_contracts DROP CONSTRAINT IF EXISTS fk_yf_contracts_business_tenant;
ALTER TABLE yeoljeong_payroll_statements DROP CONSTRAINT IF EXISTS fk_yf_payroll_business_tenant;
DROP INDEX IF EXISTS idx_yf_join_tenant_business;
DROP INDEX IF EXISTS idx_yf_documents_tenant_business;
DROP INDEX IF EXISTS idx_yf_contracts_tenant_business;
DROP INDEX IF EXISTS idx_yf_payroll_tenant_business;
DROP INDEX IF EXISTS uq_ybtm_business_tenant;
DROP TABLE IF EXISTS yeoljeong_hr_tenant_attribution_audit;
ALTER TABLE yeoljeong_employee_join_requests DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE yeoljeong_onboarding_documents DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE yeoljeong_contracts DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE yeoljeong_payroll_statements DROP COLUMN IF EXISTS tenant_id;
COMMIT;
