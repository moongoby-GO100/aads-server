BEGIN;
CREATE TABLE IF NOT EXISTS design_agent_skill_routes (
    situation TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    required BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (situation, skill_name)
);
INSERT INTO design_agent_skill_routes (situation, skill_name) VALUES
    ('S1', 'design-intake-interview'),
    ('S1', 'design-discovery'),
    ('S1', 'design-art-direction'),
    ('S1', 'design-project-onboarding'),
    ('S2', 'design-intake-interview'),
    ('S2', 'frontend-design-guide'),
    ('S2', 'design-interaction-prototype'),
    ('S3', 'design-change-impact'),
    ('S3', 'webapp-verification'),
    ('S4', 'design-intake-interview'),
    ('S4', 'design-discovery'),
    ('S4', 'design-art-direction'),
    ('S4', 'design-critique'),
    ('S5', 'design-interaction-prototype'),
    ('S5', 'design-critique'),
    ('S6', 'webapp-verification'),
    ('S6', 'design-contrast-audit'),
    ('S7', 'design-external-import'),
    ('S7', 'design-token-extraction'),
    ('S8', 'design-critique'),
    ('S8', 'document-coauthoring'),
    ('S9', 'webapp-verification'),
    ('S9', 'design-change-impact'),
    ('S9', 'design-contrast-audit')
ON CONFLICT DO NOTHING;
ALTER TABLE design_agent_runs
    ADD COLUMN IF NOT EXISTS skills_loaded JSONB NOT NULL DEFAULT '[]'::jsonb;
COMMIT;
