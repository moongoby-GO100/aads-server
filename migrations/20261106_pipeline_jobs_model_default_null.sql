-- Preserve existing rows; omitted models use automatic selection.
ALTER TABLE pipeline_jobs ALTER COLUMN model SET DEFAULT NULL;
