-- AADS already exists; preserve it and all existing human-maintained rows.
BEGIN;
INSERT INTO design_projects (tenant_id, project_key, display_name, repo_path, status, metadata)
VALUES
('2d701a8c-9596-4757-8588-faa4f7837112', 'GO100', '백억이 투자분석', '/root/kis-autotrade-v4', 'active', '{"server_id": "contabo14", "backfill_task": "AADS-DESIGN-LIFECYCLE-P0B-PROJECT-AUTOREG-20261029"}'::jsonb),
('2d701a8c-9596-4757-8588-faa4f7837112', 'KIS', 'KIS 자동매매', '/root/kis-autotrade-v4', 'active', '{"server_id": "contabo14", "backfill_task": "AADS-DESIGN-LIFECYCLE-P0B-PROJECT-AUTOREG-20261029"}'::jsonb),
('2d701a8c-9596-4757-8588-faa4f7837112', 'SF', 'ShortFlow 숏폼자동화', 'unknown', 'pending_repo', '{"server_id": "cafe24_114", "backfill_task": "AADS-DESIGN-LIFECYCLE-P0B-PROJECT-AUTOREG-20261029", "reason": "Repository path is not confirmed in project configuration"}'::jsonb),
('2d701a8c-9596-4757-8588-faa4f7837112', 'NTV2', 'NewTalk V2', '/srv/newtalk-v2', 'active', '{"server_id": "cafe24_114", "backfill_task": "AADS-DESIGN-LIFECYCLE-P0B-PROJECT-AUTOREG-20261029"}'::jsonb),
('2d701a8c-9596-4757-8588-faa4f7837112', 'NAS', 'NAS', 'unknown', 'pending_repo', '{"server_id": "cafe24_114", "backfill_task": "AADS-DESIGN-LIFECYCLE-P0B-PROJECT-AUTOREG-20261029", "reason": "Repository path is not confirmed in project configuration"}'::jsonb)
ON CONFLICT DO NOTHING;
COMMIT;
