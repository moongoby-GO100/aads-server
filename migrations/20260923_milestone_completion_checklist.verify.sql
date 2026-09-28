DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'milestones'
          AND column_name = 'completion_checklist'
          AND data_type = 'jsonb' AND is_nullable = 'YES'
          AND column_default IS NULL
    ) THEN
        RAISE EXCEPTION 'milestones.completion_checklist must be nullable jsonb with no default';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'milestones'
          AND column_name = 'completion_criteria' AND data_type = 'text'
    ) THEN
        RAISE EXCEPTION 'milestones.completion_criteria text must be preserved';
    END IF;
END $$;
