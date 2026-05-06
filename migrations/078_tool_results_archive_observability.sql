-- 078_tool_results_archive_observability.sql
-- AADS-TOOL-003: tool_results_archive 분석용 컬럼 보강

ALTER TABLE tool_results_archive
    ADD COLUMN IF NOT EXISTS result_summary TEXT,
    ADD COLUMN IF NOT EXISTS latency_ms INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS success BOOLEAN,
    ADD COLUMN IF NOT EXISTS error_detail TEXT;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'tool_results_archive'
          AND column_name = 'is_error'
    ) THEN
        EXECUTE $sql$
            UPDATE tool_results_archive
            SET success = COALESCE(success, NOT COALESCE(is_error, FALSE))
            WHERE success IS NULL
        $sql$;
    END IF;
END $$;
