-- Rollback: AADS-LLM-M9-COST-BASIS-20260930
-- 수동 적용 전용(apply_release_migrations.sh 는 rollback/ 를 건너뛴다).
-- 주의: 러너가 job_id 로 남긴 귀속 정보와 cost_source 구분이 사라진다.

-- up 에서 NULL 로 되돌린 미측정 0.00 을 복원한다.
UPDATE oauth_usage_log
   SET cost_usd = 0
 WHERE call_source IN ('anthropic_client', 'anthropic_client_msg', 'ceo_chat', 'ceo_chat_tools')
   AND cost_usd IS NULL;

-- cli_relay 의 미보고 0.00 도 옛 표기(0)로 되돌린다. 옛 코드는 NULL 을 쓰지 않았다.
UPDATE oauth_usage_log
   SET cost_usd = 0
 WHERE call_source = 'cli_relay'
   AND cost_usd IS NULL;

DROP FUNCTION IF EXISTS public.llm_catalog_cost_usd(TEXT, BIGINT, BIGINT);
DROP INDEX IF EXISTS idx_oauth_usage_job_id;
ALTER TABLE oauth_usage_log DROP CONSTRAINT IF EXISTS chk_oauth_usage_log_cost_source;
ALTER TABLE oauth_usage_log DROP COLUMN IF EXISTS job_id;
ALTER TABLE oauth_usage_log DROP COLUMN IF EXISTS cost_usd_catalog;
ALTER TABLE oauth_usage_log DROP COLUMN IF EXISTS cost_source;
