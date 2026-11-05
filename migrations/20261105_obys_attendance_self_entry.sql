-- 업무 DB obys 전용. AADS DB에는 적용하지 않는다. 운영 적용은 배포 단계에서 별도 승인 후 실행한다.
-- 직원 본인 '신규 근무 신청'(원본 근태 없음)을 기존 정정 신청 테이블에 kind 로 구분해 담는다.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'obys' THEN
        RAISE EXCEPTION 'OBYS business database required';
    END IF;
END $$;
ALTER TABLE public.obys_attendance_corrections
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'correction';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='obys_attendance_corrections_kind_check') THEN
        ALTER TABLE public.obys_attendance_corrections
            ADD CONSTRAINT obys_attendance_corrections_kind_check CHECK (kind IN ('correction','new_entry'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='obys_attendance_corrections_record_kind_check') THEN
        ALTER TABLE public.obys_attendance_corrections
            ADD CONSTRAINT obys_attendance_corrections_record_kind_check
            CHECK (kind = 'new_entry' OR record_id IS NOT NULL);
    END IF;
END $$;
-- 신규 신청은 원본 근태가 없으므로 record_id 는 승인 시 생성된 근태 id 로 채워진다.
ALTER TABLE public.obys_attendance_corrections ALTER COLUMN record_id DROP NOT NULL;
-- 같은 직원·근무일·시작시각의 대기 중 신규 신청은 하나만 허용한다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_obys_correction_new_entry_pending
    ON public.obys_attendance_corrections(business_id, lower(employee_email), (requested->>'work_date'), (requested->>'start_at'))
    WHERE kind='new_entry' AND status='pending';
COMMIT;
