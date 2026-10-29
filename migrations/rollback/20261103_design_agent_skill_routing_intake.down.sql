BEGIN;
DELETE FROM design_agent_skill_routes
WHERE situation = 'S3' AND skill_name = 'design-intake-interview';
COMMIT;
