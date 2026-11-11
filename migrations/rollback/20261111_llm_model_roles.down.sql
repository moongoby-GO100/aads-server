-- 롤백: llm_model_roles 표 제거
--
-- 순서:
--   1. 백업: 표 DROP 전 필요한 경우 데이터 백업을 먼저 수행한다 (예: pg_dump -t llm_model_roles).
--   2. 코드 먼저 되돌리기: 애플리케이션 코드를 llm_model_roles 참조 이전 버전으로 먼저 되돌린다 (코드 롤백 배포).
--   3. 표 제거: 아래 DROP TABLE IF EXISTS llm_model_roles 를 실행한다 (코드는 표가 없어도 model_roles_seed.py 로 안전하게 폴백).
--
SET lock_timeout = '5s';
SET statement_timeout = '60s';
BEGIN;
DROP TABLE IF EXISTS llm_model_roles;
COMMIT;
