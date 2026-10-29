-- Delete only rows inserted by this backfill; never the pre-existing AADS row.
BEGIN;
DELETE FROM design_projects
WHERE tenant_id = '2d701a8c-9596-4757-8588-faa4f7837112'
  AND project_key IN ('GO100', 'KIS', 'SF', 'NTV2', 'NAS')
  AND metadata->>'backfill_task' = 'AADS-DESIGN-LIFECYCLE-P0B-PROJECT-AUTOREG-20261029';
COMMIT;
