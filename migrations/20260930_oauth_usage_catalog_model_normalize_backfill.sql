-- AADS-COST-CATALOG-MODELNAME-NORMALIZE-BACKFILL-20260930
-- oauth_usage_log.cost_usd_catalog 가 7일 창에서 97% NULL 이던 두 원인을 함께 고친다.
--
--   원인 A  Codex 릴레이가 model 컬럼에 표시명("GPT-6 Sol (Codex CLI)")을 적어
--           llm_catalog_cost_usd() 의 model_id 정확일치가 실패했다.
--   원인 B  20260930_oauth_usage_cost_basis 이전에 적힌 행은 cost_usd_catalog 가 비어 있다.
--
-- 1) llm_resolve_model_id(text)  표시명 → model_id.
--      ① model_id 정확일치(기존 동작 그대로)  ② llm_models.display_name 역조회
--      ③ 보조 규칙: "codex:" 접두·"(… CLI)" 꼬리표를 떼고 소문자·공백→'-' 한 슬러그가
--         카탈로그 model_id 와 같을 때만.  못 풀면 NULL — 추측해 만들어 내지 않는다.
-- 2) llm_catalog_cost_usd()  조인 키만 resolver 결과로 바꾼다. 정확일치 행은 resolver 가
--    그 model_id 를 그대로 돌려주므로 결과가 달라지지 않는다. 단가가 없으면 NULL(0 아님).
-- 3) 백필  cost_usd_catalog IS NULL 이고 정규화 후 단가가 있는 행만 UPDATE.
--
-- 백필 표지를 cost_source 에 넣지 않은 이유(실측):
--   cost_source 는 chk_oauth_usage_log_cost_source(relay_reported/catalog_estimated/unknown)
--   로 막혀 있고, 그 값은 **cost_usd 의 출처**다. 백필 표지로 덮어쓰면 relay_reported 행의
--   보고 비용이 llm_cost_basis 요약에서 다른 그룹으로 빠진다. 그래서 별도 열
--   catalog_backfill_tag 에 남기고 cost_source 는 건드리지 않는다.
--   되돌릴 때는 catalog_backfill_tag 로 골라 cost_usd_catalog 를 NULL 로 되돌린다.
--
-- model 컬럼은 고치지 않는다(원본 보존). 정규화는 함수가 읽을 때 한다.
-- 파괴적 문장 없음(UPDATE 와 함수 교체만). 각 문장은 이 파일이 실행되는 트랜잭션 안에서 돈다.

ALTER TABLE oauth_usage_log
    ADD COLUMN IF NOT EXISTS catalog_backfill_tag VARCHAR(60);

CREATE OR REPLACE FUNCTION public.llm_resolve_model_id(
    p_model TEXT
) RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(
        (SELECT m.model_id
           FROM llm_models m
          WHERE m.model_id = split_part(COALESCE(p_model, ''), '[', 1)
          ORDER BY (m.input_cost IS NOT NULL AND m.output_cost IS NOT NULL) DESC,
                   (m.provider = 'anthropic') DESC,
                   m.is_active DESC NULLS LAST,
                   m.id
          LIMIT 1),
        (SELECT m.model_id
           FROM llm_models m
          WHERE m.display_name = btrim(split_part(COALESCE(p_model, ''), '[', 1))
          ORDER BY (m.input_cost IS NOT NULL AND m.output_cost IS NOT NULL) DESC,
                   (m.provider = 'anthropic') DESC,
                   m.is_active DESC NULLS LAST,
                   m.id
          LIMIT 1),
        (SELECT m.model_id
           FROM llm_models m
          WHERE m.model_id = lower(regexp_replace(
                    regexp_replace(
                        regexp_replace(btrim(split_part(COALESCE(p_model, ''), '[', 1)),
                                       '^codex:', '', 'i'),
                        '\s*\([^)]*\mcli\M[^)]*\)\s*$', '', 'i'),
                    '\s+', '-', 'g'))
          ORDER BY (m.input_cost IS NOT NULL AND m.output_cost IS NOT NULL) DESC,
                   (m.provider = 'anthropic') DESC,
                   m.is_active DESC NULLS LAST,
                   m.id
          LIMIT 1)
    )
$$;

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
     WHERE m.model_id = COALESCE(public.llm_resolve_model_id(p_model),
                                 split_part(COALESCE(p_model, ''), '[', 1))
       AND m.input_cost IS NOT NULL
       AND m.output_cost IS NOT NULL
     ORDER BY (m.provider = 'anthropic') DESC,
              m.is_active DESC NULLS LAST,
              m.id
     LIMIT 1
$$;

-- 과거 행 백필. 모델 종류별 단가를 한 번만 풀고(행마다 함수를 부르면 44k행에서 분 단위로
-- 느려진다), id 구간(5000)으로 나눠 UPDATE 하며 구간마다 건수를 NOTICE 로 남긴다.
-- 단가는 함수 자신으로 뽑는다: llm_catalog_cost_usd(m, 1e6, 0) = input_cost 그대로,
-- llm_catalog_cost_usd(m, 0, 1e6) = output_cost 그대로. 단가 선택 규칙을 여기에 복제하지 않는다.
-- 단가가 없는 모델은 NULL 이라 WHERE 에서 걸러져 NULL 로 남는다.
DO $backfill$
DECLARE
    v_step   CONSTANT BIGINT := 5000;
    v_from   BIGINT := 0;
    v_max    BIGINT;
    v_n      BIGINT;
    v_total  BIGINT := 0;
    v_null_before BIGINT;
    v_null_after  BIGINT;
BEGIN
    SELECT COALESCE(MAX(id), 0) INTO v_max FROM oauth_usage_log;
    SELECT COUNT(*) INTO v_null_before FROM oauth_usage_log WHERE cost_usd_catalog IS NULL;
    RAISE NOTICE 'catalog_backfill before: cost_usd_catalog NULL rows = %', v_null_before;

    CREATE TEMP TABLE _catalog_backfill_rate ON COMMIT DROP AS
        SELECT d.model,
               public.llm_catalog_cost_usd(d.model, 1000000, 0) AS in_rate,
               public.llm_catalog_cost_usd(d.model, 0, 1000000) AS out_rate
          FROM (SELECT DISTINCT model FROM oauth_usage_log WHERE cost_usd_catalog IS NULL) d;

    WHILE v_from < v_max LOOP
        UPDATE oauth_usage_log o
           SET cost_usd_catalog = ROUND(
                   (COALESCE(o.input_tokens, 0) * r.in_rate
                    + COALESCE(o.output_tokens, 0) * r.out_rate) / 1000000.0, 6),
               catalog_backfill_tag = 'model_normalize_backfill_20260930'
          FROM _catalog_backfill_rate r
         WHERE o.model = r.model
           AND o.id > v_from
           AND o.id <= v_from + v_step
           AND o.cost_usd_catalog IS NULL
           AND o.catalog_backfill_tag IS NULL
           AND r.in_rate IS NOT NULL
           AND r.out_rate IS NOT NULL;
        GET DIAGNOSTICS v_n = ROW_COUNT;
        v_total := v_total + v_n;
        RAISE NOTICE 'catalog_backfill batch id (%, %] updated %', v_from, v_from + v_step, v_n;
        v_from := v_from + v_step;
    END LOOP;

    SELECT COUNT(*) INTO v_null_after FROM oauth_usage_log WHERE cost_usd_catalog IS NULL;
    RAISE NOTICE 'catalog_backfill after: updated % rows, NULL rows = % (left NULL: no catalog price)',
        v_total, v_null_after;
END
$backfill$;
