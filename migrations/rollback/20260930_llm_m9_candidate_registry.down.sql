-- Rollback of migrations/20260930_llm_m9_candidate_registry.sql. llm_models 는 건드리지 않는다.
BEGIN;
DROP TABLE IF EXISTS llm_model_comparisons;
DROP TRIGGER IF EXISTS trg_llm_model_candidates_enforce_exclusion ON llm_model_candidates;
DROP TABLE IF EXISTS llm_model_candidates;
DROP FUNCTION IF EXISTS llm_model_candidates_enforce_exclusion();
COMMIT;
