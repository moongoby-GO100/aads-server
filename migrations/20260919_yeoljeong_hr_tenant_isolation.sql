-- O2 HR attribution is deliberately deterministic and non-destructive.
-- Priority: relational business -> explicit FK -> non-conflicting legacy JSON
-- -> exactly one mapped business seen on authoritative rows for normalized email.
BEGIN;

ALTER TABLE yeoljeong_employee_join_requests ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE yeoljeong_onboarding_documents ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE yeoljeong_contracts ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE yeoljeong_payroll_statements ADD COLUMN IF NOT EXISTS tenant_id UUID;

CREATE TABLE IF NOT EXISTS yeoljeong_hr_tenant_attribution_audit (
    ledger_table TEXT NOT NULL,
    row_id TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (classification IN ('attributed', 'unresolved', 'conflict')),
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    business_id TEXT,
    tenant_id UUID,
    classified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ledger_table, row_id)
);

CREATE TEMP TABLE _o2_hr_rows ON COMMIT DROP AS
SELECT 'yeoljeong_employee_join_requests'::text ledger_table, id::text row_id,
       lower(trim(employee_email)) normalized_email, nullif(trim(business_id), '') relational_business,
       null::text fk_business, nullif(trim(request_payload->>'business_id'), '') legacy_business
  FROM yeoljeong_employee_join_requests WHERE deleted_at IS NULL
UNION ALL
SELECT 'yeoljeong_onboarding_documents', d.id::text, lower(trim(d.employee_email)),
       nullif(trim(d.business_id), ''), nullif(trim(j.business_id), ''),
       nullif(trim(d.metadata->>'business_id'), '')
  FROM yeoljeong_onboarding_documents d
  LEFT JOIN yeoljeong_employee_join_requests j
    ON j.id::text = d.employee_request_id::text AND j.deleted_at IS NULL
 WHERE d.deleted_at IS NULL
UNION ALL
SELECT 'yeoljeong_contracts', id::text, lower(trim(employee_email)), nullif(trim(business_id), ''),
       null::text, nullif(trim(contract_payload->>'business_id'), '')
  FROM yeoljeong_contracts WHERE deleted_at IS NULL
UNION ALL
SELECT 'yeoljeong_payroll_statements', id::text, lower(trim(employee_email)), nullif(trim(business_id), ''),
       null::text, nullif(trim(statement_payload->>'business_id'), '')
  FROM yeoljeong_payroll_statements WHERE deleted_at IS NULL;

CREATE TEMP TABLE _o2_email_candidates ON COMMIT DROP AS
SELECT r.normalized_email, array_agg(DISTINCT r.relational_business ORDER BY r.relational_business) businesses
  FROM _o2_hr_rows r
  JOIN yeoljeong_business_tenant_mapping m ON m.business_id = r.relational_business
 WHERE r.normalized_email <> '' AND r.relational_business IS NOT NULL
 GROUP BY r.normalized_email;

CREATE TEMP TABLE _o2_classification ON COMMIT DROP AS
WITH signals AS (
    SELECT r.*,
           mr.tenant_id relational_tenant,
           mf.tenant_id fk_tenant,
           ml.tenant_id legacy_tenant,
           ec.businesses email_businesses
      FROM _o2_hr_rows r
      LEFT JOIN yeoljeong_business_tenant_mapping mr ON mr.business_id = r.relational_business
      LEFT JOIN yeoljeong_business_tenant_mapping mf ON mf.business_id = r.fk_business
      LEFT JOIN yeoljeong_business_tenant_mapping ml ON ml.business_id = r.legacy_business
      LEFT JOIN _o2_email_candidates ec ON ec.normalized_email = r.normalized_email
), classified AS (
    SELECT s.*,
           CASE
             WHEN relational_tenant IS NOT NULL THEN relational_business
             WHEN fk_tenant IS NOT NULL THEN fk_business
             WHEN legacy_tenant IS NOT NULL
                  AND (relational_business IS NULL OR relational_business = legacy_business)
                  AND (fk_business IS NULL OR fk_business = legacy_business)
                  AND (email_businesses IS NULL OR email_businesses <@ ARRAY[legacy_business]) THEN legacy_business
             WHEN relational_business IS NULL AND fk_business IS NULL AND cardinality(email_businesses) = 1 THEN email_businesses[1]
           END chosen_business,
           CASE
             WHEN legacy_tenant IS NOT NULL AND (
                    (relational_business IS NOT NULL AND relational_business <> legacy_business)
                 OR (fk_business IS NOT NULL AND fk_business <> legacy_business)
                 OR (email_businesses IS NOT NULL AND NOT email_businesses <@ ARRAY[legacy_business])
                  ) THEN 'legacy_conflicts_authoritative_signal'
             WHEN relational_business IS NOT NULL AND relational_tenant IS NULL THEN 'unmapped_relational_business'
             WHEN fk_business IS NOT NULL AND fk_tenant IS NULL THEN 'unmapped_fk_business'
             WHEN cardinality(email_businesses) > 1 THEN 'ambiguous_authoritative_email'
             WHEN relational_tenant IS NOT NULL THEN 'mapped_relational_business'
             WHEN fk_tenant IS NOT NULL THEN 'mapped_explicit_fk'
             WHEN legacy_tenant IS NOT NULL THEN 'mapped_nonconflicting_legacy_payload'
             WHEN cardinality(email_businesses) = 1 THEN 'unique_authoritative_email'
             ELSE 'no_mapped_candidate'
           END reason,
           CASE
             WHEN relational_tenant IS NOT NULL THEN 'relational_business_id'
             WHEN fk_tenant IS NOT NULL THEN 'explicit_fk'
             WHEN legacy_tenant IS NOT NULL
                  AND (relational_business IS NULL OR relational_business = legacy_business)
                  AND (fk_business IS NULL OR fk_business = legacy_business)
                  AND (email_businesses IS NULL OR email_businesses <@ ARRAY[legacy_business]) THEN 'legacy_json_once'
             WHEN relational_business IS NULL AND fk_business IS NULL AND cardinality(email_businesses) = 1 THEN 'normalized_email_unique'
             ELSE 'none'
           END source
      FROM signals s
)
SELECT c.*,
       m.tenant_id chosen_tenant,
       CASE WHEN c.chosen_business IS NOT NULL THEN 'attributed'
            WHEN c.reason LIKE '%conflict%' OR c.reason LIKE 'ambiguous%' THEN 'conflict'
            ELSE 'unresolved' END classification
  FROM classified c
  LEFT JOIN yeoljeong_business_tenant_mapping m ON m.business_id = c.chosen_business;

INSERT INTO yeoljeong_hr_tenant_attribution_audit
    (ledger_table, row_id, classification, reason, source, business_id, tenant_id, classified_at)
SELECT ledger_table, row_id, classification, reason, source, chosen_business, chosen_tenant, now()
  FROM _o2_classification
ON CONFLICT (ledger_table, row_id) DO UPDATE SET
    classification = EXCLUDED.classification, reason = EXCLUDED.reason, source = EXCLUDED.source,
    business_id = EXCLUDED.business_id, tenant_id = EXCLUDED.tenant_id, classified_at = EXCLUDED.classified_at;

UPDATE yeoljeong_employee_join_requests r SET business_id = c.chosen_business, tenant_id = c.chosen_tenant
  FROM _o2_classification c WHERE c.ledger_table = 'yeoljeong_employee_join_requests'
   AND c.row_id = r.id::text AND c.classification = 'attributed' AND r.tenant_id IS NULL;
UPDATE yeoljeong_onboarding_documents r SET business_id = c.chosen_business, tenant_id = c.chosen_tenant
  FROM _o2_classification c WHERE c.ledger_table = 'yeoljeong_onboarding_documents'
   AND c.row_id = r.id::text AND c.classification = 'attributed' AND r.tenant_id IS NULL;
UPDATE yeoljeong_contracts r SET business_id = c.chosen_business, tenant_id = c.chosen_tenant
  FROM _o2_classification c WHERE c.ledger_table = 'yeoljeong_contracts'
   AND c.row_id = r.id::text AND c.classification = 'attributed' AND r.tenant_id IS NULL;
UPDATE yeoljeong_payroll_statements r SET business_id = c.chosen_business, tenant_id = c.chosen_tenant
  FROM _o2_classification c WHERE c.ledger_table = 'yeoljeong_payroll_statements'
   AND c.row_id = r.id::text AND c.classification = 'attributed' AND r.tenant_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ybtm_business_tenant ON yeoljeong_business_tenant_mapping (business_id, tenant_id);
CREATE INDEX IF NOT EXISTS idx_yf_join_tenant_business ON yeoljeong_employee_join_requests (tenant_id, business_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_yf_documents_tenant_business ON yeoljeong_onboarding_documents (tenant_id, business_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_yf_contracts_tenant_business ON yeoljeong_contracts (tenant_id, business_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_yf_payroll_tenant_business ON yeoljeong_payroll_statements (tenant_id, business_id) WHERE deleted_at IS NULL;

DO $$ BEGIN
  ALTER TABLE yeoljeong_employee_join_requests ADD CONSTRAINT fk_yf_join_business_tenant FOREIGN KEY (business_id, tenant_id) REFERENCES yeoljeong_business_tenant_mapping (business_id, tenant_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE yeoljeong_onboarding_documents ADD CONSTRAINT fk_yf_documents_business_tenant FOREIGN KEY (business_id, tenant_id) REFERENCES yeoljeong_business_tenant_mapping (business_id, tenant_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE yeoljeong_contracts ADD CONSTRAINT fk_yf_contracts_business_tenant FOREIGN KEY (business_id, tenant_id) REFERENCES yeoljeong_business_tenant_mapping (business_id, tenant_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE yeoljeong_payroll_statements ADD CONSTRAINT fk_yf_payroll_business_tenant FOREIGN KEY (business_id, tenant_id) REFERENCES yeoljeong_business_tenant_mapping (business_id, tenant_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ DECLARE r RECORD; BEGIN
  FOR r IN SELECT classification, count(*) count FROM yeoljeong_hr_tenant_attribution_audit GROUP BY classification ORDER BY classification LOOP
    RAISE NOTICE 'O2 HR attribution: classification=%, count=%', r.classification, r.count;
  END LOOP;
END $$;

COMMIT;
