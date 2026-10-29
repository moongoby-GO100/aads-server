BEGIN;
INSERT INTO design_agent_skill_routes (situation, skill_name) VALUES
    ('S3', 'design-intake-interview')
ON CONFLICT DO NOTHING;
COMMIT;
