-- 업무 DB obys 전용. AADS DB에는 적용하지 않는다.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'obys' THEN
        RAISE EXCEPTION 'OBYS business database required';
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS public.obys_attendance_corrections (
    id TEXT PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id),
    record_id TEXT NOT NULL,
    employee_email TEXT NOT NULL,
    original JSONB NOT NULL,
    requested JSONB NOT NULL,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    supersedes_id TEXT REFERENCES public.obys_attendance_corrections(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','withdrawn')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    decision_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_obys_correction_pending
    ON public.obys_attendance_corrections(business_id,record_id) WHERE status='pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_obys_correction_supersedes
    ON public.obys_attendance_corrections(supersedes_id) WHERE supersedes_id IS NOT NULL;
-- Every payroll writer participates, including inserts for an empty month.
-- Acquiring keys in stable order also covers a payroll row moved between businesses.
CREATE OR REPLACE FUNCTION public.obys_correction_payroll_fence() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE key_business text;
BEGIN
    FOR key_business IN
        SELECT DISTINCT b FROM unnest(ARRAY[NEW.business_id,
            CASE WHEN TG_OP = 'UPDATE' THEN OLD.business_id ELSE NEW.business_id END]) b ORDER BY b
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('correction:' || key_business, 0));
    END LOOP;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.obys_correction_payroll_fence() FROM PUBLIC;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.yeoljeong_payroll_statements'::regclass
                   AND tgname='obys_correction_payroll_fence') THEN
        CREATE TRIGGER obys_correction_payroll_fence BEFORE INSERT OR UPDATE
            ON public.yeoljeong_payroll_statements FOR EACH ROW
            EXECUTE FUNCTION public.obys_correction_payroll_fence();
    END IF;
END $$;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='acct_business_runtime_r5') THEN
        GRANT SELECT,INSERT,UPDATE ON public.obys_attendance_corrections TO acct_business_runtime_r5;
    END IF;
END $$;
COMMIT;
