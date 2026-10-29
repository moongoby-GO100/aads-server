BEGIN;
DROP TABLE IF EXISTS design_agent_skill_routes;
ALTER TABLE design_agent_runs DROP COLUMN IF EXISTS skills_loaded;
COMMIT;
