-- [C안 3/4] project label normalization for memory_facts / project_tasks
-- Purpose: keep user-facing workspace labels separate from canonical project keys.
-- Safety: backs up every touched row before updating; no delete/drop/truncate.

BEGIN;

CREATE TABLE IF NOT EXISTS project_label_normalization_backup_20260820 (
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    old_project VARCHAR(255),
    new_project VARCHAR(255),
    reason TEXT NOT NULL,
    backup_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (table_name, row_id, reason)
);

-- memory_facts: '[TOKEN] label' -> 'TOKEN'
INSERT INTO project_label_normalization_backup_20260820 (
    table_name, row_id, old_project, new_project, reason
)
SELECT
    'memory_facts',
    id::text,
    project,
    UPPER(TRIM((regexp_match(project, '^\[([^\]]+)\]'))[1])),
    'bracket_token'
FROM memory_facts
WHERE project ~ '^\[[^\]]+\]'
  AND project IS DISTINCT FROM UPPER(TRIM((regexp_match(project, '^\[([^\]]+)\]'))[1]))
ON CONFLICT DO NOTHING;

UPDATE memory_facts
SET project = UPPER(TRIM((regexp_match(project, '^\[([^\]]+)\]'))[1])),
    updated_at = NOW()
WHERE project ~ '^\[[^\]]+\]'
  AND project IS DISTINCT FROM UPPER(TRIM((regexp_match(project, '^\[([^\]]+)\]'))[1]));

-- project_tasks: historical display names -> canonical keys
INSERT INTO project_label_normalization_backup_20260820 (
    table_name, row_id, old_project, new_project, reason
)
SELECT
    'project_tasks',
    id::text,
    project,
    CASE
        WHEN LOWER(project) = 'shortflow' THEN 'SF'
        WHEN LOWER(project) = 'newtalk' THEN 'NTV2'
    END,
    'legacy_display_name'
FROM project_tasks
WHERE LOWER(project) IN ('shortflow', 'newtalk')
  AND project IS DISTINCT FROM CASE
        WHEN LOWER(project) = 'shortflow' THEN 'SF'
        WHEN LOWER(project) = 'newtalk' THEN 'NTV2'
    END
ON CONFLICT DO NOTHING;

UPDATE project_tasks
SET project = CASE
        WHEN LOWER(project) = 'shortflow' THEN 'SF'
        WHEN LOWER(project) = 'newtalk' THEN 'NTV2'
        ELSE project
    END,
    updated_at = NOW()
WHERE LOWER(project) IN ('shortflow', 'newtalk');

COMMIT;
