-- Restore the previous default without modifying existing rows.
ALTER TABLE pipeline_jobs ALTER COLUMN model SET DEFAULT 'claude-sonnet-4-6';
