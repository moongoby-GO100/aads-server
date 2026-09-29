-- AADS-LLM-M9-COST-BASIS-20260930
-- oauth_usage_log 를 토큰·호출·비용의 정본으로 확정하고, 비용의 출처를 분리한다.
--
--   cost_source       cost_usd 가 어디서 온 값인가
--                       relay_reported    CLI/릴레이가 스스로 보고한 total_cost_usd
--                       catalog_estimated 앱이 단가표로 계산해 넣은 추정값
--                       unknown           측정하지 않음(cost_usd 는 NULL)
--   cost_usd_catalog  기록 시점 llm_models 정가(input/output per 1M)로 다시 계산한 값.
--                     cost_usd 와 **절대 같은 컬럼에 섞지 않는다.** 카탈로그에 캐시
--                     단가가 없으므로 캐시 토큰은 넣지 않는다(추측 배율 금지).
--   job_id            러너 실행 컨텍스트의 pipeline_jobs.job_id. session_id 는
--                     그대로 두고 따로 남긴다 — 러너 비용을 러너 작업에 귀속하기 위함.
--
-- 기존 행 백필 원칙: 값은 추정해서 채우지 않는다. cost_usd_catalog 는 NULL 로 둔다.
-- cost_source 는 기록 코드가 실제로 넣던 값의 출처를 call_source 별로 표시한다.
--   cli_relay, 값 ≠ 0   → relay_reported    (model_selector: event.total_cost_usd)
--   cli_relay, 값 = 0   → unknown, NULL     (옛 코드가 float(total_cost_usd or 0) 으로
--                                            적었다 — CLI 가 비용을 보고하지 않은 행과
--                                            구분되지 않는다. 0 을 보고값으로 표시하지
--                                            않는다. rework 2, 리뷰 지적 5)
--   codex_relay         → catalog_estimated (model_selector: _estimate_cost(_COST_MAP))
--   그 외               → unknown           (model_selector_sdk 는 보고값/추정값이
--                                            행 단위로 구분되지 않는다. anthropic_client·
--                                            ceo_chat 은 비용을 넘기지 않아 0 으로 적혔다)
-- 지시서 초안은 "기존 행 전부 relay_reported" 였으나, codex_relay 와 anthropic_client
-- 행은 코드상 릴레이 보고값이 아니므로 그렇게 표시하면 불일치를 오히려 감춘다.
-- 측정하지 않은 0.00 은 NULL 로 되돌린다(미측정을 미측정으로 표시).

ALTER TABLE oauth_usage_log
    ADD COLUMN IF NOT EXISTS cost_source VARCHAR(20) NOT NULL DEFAULT 'unknown';
ALTER TABLE oauth_usage_log
    ADD COLUMN IF NOT EXISTS cost_usd_catalog NUMERIC(12,6);
ALTER TABLE oauth_usage_log
    ADD COLUMN IF NOT EXISTS job_id VARCHAR(100);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_oauth_usage_log_cost_source'
           AND conrelid = 'public.oauth_usage_log'::regclass
    ) THEN
        ALTER TABLE public.oauth_usage_log
            ADD CONSTRAINT chk_oauth_usage_log_cost_source
            CHECK (cost_source IN ('relay_reported', 'catalog_estimated', 'unknown'));
    END IF;
END $$;

-- 백필: 이 마이그레이션이 추가한 기본값('unknown') 인 행만 건드린다. 재실행해도 같다.
UPDATE oauth_usage_log
   SET cost_source = 'relay_reported'
 WHERE call_source = 'cli_relay'
   AND cost_usd <> 0
   AND cost_source = 'unknown'
   AND cost_usd_catalog IS NULL
   AND job_id IS NULL;

UPDATE oauth_usage_log
   SET cost_source = 'catalog_estimated'
 WHERE call_source = 'codex_relay'
   AND cost_source = 'unknown'
   AND cost_usd_catalog IS NULL
   AND job_id IS NULL;

-- 미측정을 NULL 로 적으려면 cost_usd 가 NULL 을 받아야 한다(리뷰 지적 4, rework 1).
-- 042 는 `DECIMAL(10,6) DEFAULT 0` 으로 만들었고 NOT NULL 이 없다. 2026-09-30 운영 DB
-- 실측도 is_nullable=YES, CHECK·트리거 없음. 다른 환경에서 누가 NOT NULL 을 걸었더라도
-- 아래 UPDATE 와 앱 INSERT(cost_usd=NULL) 가 깨지지 않도록 여기서 보증한다.
-- 이미 nullable 이면 아무것도 하지 않는다(멱등). 기본값 0 은 건드리지 않는다 —
-- 앱은 항상 값을 명시하므로 기본값은 옛 호출자에게만 쓰인다.
ALTER TABLE oauth_usage_log
    ALTER COLUMN cost_usd DROP NOT NULL;

UPDATE oauth_usage_log
   SET cost_usd = NULL
 WHERE call_source IN ('anthropic_client', 'anthropic_client_msg', 'ceo_chat', 'ceo_chat_tools')
   AND cost_source = 'unknown'
   AND cost_usd = 0;

-- cli_relay 의 0.00 은 위 relay_reported 백필에서 빠져 unknown 으로 남았다.
-- 보고되지 않은 비용이므로 NULL 로 적는다(2026-09-30 운영 실측 2행, 전부 토큰 0).
UPDATE oauth_usage_log
   SET cost_usd = NULL
 WHERE call_source = 'cli_relay'
   AND cost_source = 'unknown'
   AND cost_usd = 0;

CREATE INDEX IF NOT EXISTS idx_oauth_usage_job_id
    ON oauth_usage_log (job_id, created_at DESC)
    WHERE job_id IS NOT NULL;

-- 정가 재계산 함수. 기록 시점 카탈로그를 읽는다(앱 INSERT·러너 INSERT 공용).
-- 모델이 카탈로그에 없거나 단가가 비어 있으면 NULL — 0 이 아니다.
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
