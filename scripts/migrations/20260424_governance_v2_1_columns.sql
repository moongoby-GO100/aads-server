-- v2.1 P1-D: 거버넌스 컬럼 확장
-- W1-C2: intent_policies temperature 컬럼
ALTER TABLE intent_policies ADD COLUMN IF NOT EXISTS temperature NUMERIC(3,2) DEFAULT NULL;
COMMENT ON COLUMN intent_policies.temperature IS 'intent별 LLM temperature 권장값 (NULL=기본값 사용)';

-- Q14: role_profiles 프로젝트 격리
ALTER TABLE role_profiles ADD COLUMN IF NOT EXISTS project_scope TEXT[] DEFAULT NULL;
COMMENT ON COLUMN role_profiles.project_scope IS '역할 적용 프로젝트 화이트리스트 (NULL=전체, [AADS,KIS]=일부)';

-- 시드 업데이트: 기본 temperature 값
UPDATE intent_policies SET temperature=0.1 WHERE intent IN ('greeting','casual','status_check','task_query','health_check','runner_response') AND temperature IS NULL;
UPDATE intent_policies SET temperature=0.3 WHERE intent IN ('search','fact_check','knowledge_query') AND temperature IS NULL;
UPDATE intent_policies SET temperature=0.5 WHERE intent IN ('report','audit','deep_research','cto_strategy','url_analyze') AND temperature IS NULL;
UPDATE intent_policies SET temperature=0.2 WHERE intent IN ('code_modify','deploy','pipeline','git_ops') AND temperature IS NULL;
