-- Rollback: AADS-COST-CATALOG-MODELNAME-NORMALIZE-BACKFILL-20260930
-- 수동 적용 전용(apply_release_migrations.sh 는 rollback/ 를 건너뛴다).
-- 백필분만 catalog_backfill_tag 로 골라 되돌린다. 기록 시점에 함수가 채운 행은 건드리지 않는다.

UPDATE oauth_usage_log
   SET cost_usd_catalog = NULL,
       catalog_backfill_tag = NULL
 WHERE catalog_backfill_tag = 'model_normalize_backfill_20260930';

-- 정확일치 전용이던 20260930_oauth_usage_cost_basis 의 함수 본문으로 복원한다.
CREATE OR REPLACE FUNCTION public.llm_catalog_cost_usd(
    p_model TEXT,
    p_input_tokens BIGINT,
    p_output_tokens BIGINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
AS $$
    SELECT ROUND(
               (COALESCE(p_input_tokens, 0) * m.input_cost
                + COALESCE(p_output_tokens, 0) * m.output_cost) / 1000000.0,
               6)
      FROM llm_models m
     WHERE m.model_id = split_part(COALESCE(p_model, ''), '[', 1)
       AND m.input_cost IS NOT NULL
       AND m.output_cost IS NOT NULL
     ORDER BY (m.provider = 'anthropic') DESC,
              m.is_active DESC NULLS LAST,
              m.id
     LIMIT 1
$$;

DROP FUNCTION IF EXISTS public.llm_resolve_model_id(TEXT);
ALTER TABLE oauth_usage_log DROP COLUMN IF EXISTS catalog_backfill_tag;
