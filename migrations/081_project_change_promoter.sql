-- 081: Project Change Promoter memory indexes
-- Important runner/code changes are promoted into memory_facts so new sessions
-- can preload architecture/function/API/data-model changes automatically.

CREATE INDEX IF NOT EXISTS idx_memory_facts_tags_gin
    ON memory_facts USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_memory_facts_strategic_changes
    ON memory_facts (project, category, created_at DESC)
    WHERE category IN (
        'architecture_decision',
        'feature_change',
        'api_contract',
        'data_model_change'
    )
      AND superseded_by IS NULL;
