-- Additive preparation for structured milestone completion criteria.
ALTER TABLE public.milestones
    ADD COLUMN IF NOT EXISTS completion_checklist JSONB;
