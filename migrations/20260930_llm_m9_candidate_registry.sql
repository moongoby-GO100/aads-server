-- M9 후보 모델 대장 + 비교 이력. 추가 전용(기존 테이블 불변), 여러 번 실행해도 결과가 같다.
-- 현행 모델은 llm_models 가 정본이다. 여기에는 후보만 넣고, 비교 시 incumbent_* 로 참조한다.
-- 게이트는 DB 에 둔다(서비스 계층 app/services/llm_candidate_registry.py 는 같은 규칙의 사본).
--   1) training_use='used_for_training' → excluded_from_private_eval 자동 true (트리거 + CHECK)
--   2) pricing_kind 가 다른 금액 칸을 한 행에 섞지 못한다 (CHECK)
--   3) verdict 는 noninferiority_margin 이 먼저 채워진 행에만 쓸 수 있다 (CHECK)
--   4) sample_size < min_sample_size 이면 verdict 는 insufficient_sample/not_run 만 (CHECK)
BEGIN;

CREATE TABLE IF NOT EXISTS llm_model_candidates (
    id                           BIGSERIAL PRIMARY KEY,
    provider                     TEXT NOT NULL,
    model_id                     TEXT NOT NULL,
    product_name                 TEXT NOT NULL,
    surface_scope                TEXT[] NOT NULL DEFAULT '{}',
    official_url                 TEXT,
    verified_at                  TIMESTAMPTZ,
    model_version                TEXT,
    region_scope                 TEXT,
    quota_rpm                    INTEGER,
    quota_tpm                    BIGINT,
    price_input_per_1m           NUMERIC(14,6),
    price_cached_input_per_1m    NUMERIC(14,6),
    price_output_per_1m          NUMERIC(14,6),
    price_currency               TEXT NOT NULL DEFAULT 'USD',
    subscription_price_per_month NUMERIC(14,2),
    price_status                 TEXT NOT NULL DEFAULT 'estimated',
    pricing_kind                 TEXT NOT NULL,
    training_use                 TEXT NOT NULL DEFAULT 'unknown',
    training_terms_url           TEXT,
    retention_note               TEXT,
    extra_charges                JSONB NOT NULL DEFAULT '{}'::jsonb,
    excluded_from_private_eval   BOOLEAN NOT NULL DEFAULT false,
    exclusion_reason             TEXT,
    status                       TEXT NOT NULL DEFAULT 'candidate',
    notes                        TEXT,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_llm_model_candidates_provider_model UNIQUE (provider, model_id),
    CONSTRAINT ck_llm_model_candidates_surface
        CHECK (surface_scope <@ ARRAY['chat','runner','terminal_cli','service']::TEXT[]),
    CONSTRAINT ck_llm_model_candidates_price_status
        CHECK (price_status IN ('official_verified','unverified_official','estimated')),
    CONSTRAINT ck_llm_model_candidates_pricing_kind
        CHECK (pricing_kind IN ('api_per_token','subscription','per_image','per_minute')),
    CONSTRAINT ck_llm_model_candidates_training_use
        CHECK (training_use IN ('not_used','used_for_training','unknown')),
    CONSTRAINT ck_llm_model_candidates_status
        CHECK (status IN ('candidate','testing','approved','rejected','retired')),
    -- 검증됐다고 적으려면 출처와 확인 시각이 있어야 한다.
    CONSTRAINT ck_llm_model_candidates_verified_source
        CHECK (price_status <> 'official_verified' OR (official_url IS NOT NULL AND verified_at IS NOT NULL)),
    -- 구독료와 토큰 단가는 서로 다른 축이다. 한 행에 섞어 적지 않는다.
    CONSTRAINT ck_llm_model_candidates_price_axis
        CHECK (
            (pricing_kind = 'subscription'
                AND price_input_per_1m IS NULL AND price_cached_input_per_1m IS NULL
                AND price_output_per_1m IS NULL)
            OR (pricing_kind <> 'subscription' AND subscription_price_per_month IS NULL)
        ),
    CONSTRAINT ck_llm_model_candidates_training_exclusion
        CHECK (training_use <> 'used_for_training' OR excluded_from_private_eval),
    CONSTRAINT ck_llm_model_candidates_quota
        CHECK ((quota_rpm IS NULL OR quota_rpm >= 0) AND (quota_tpm IS NULL OR quota_tpm >= 0))
);

CREATE OR REPLACE FUNCTION llm_model_candidates_enforce_exclusion()
RETURNS trigger AS $$
BEGIN
    IF NEW.training_use = 'used_for_training' THEN
        NEW.excluded_from_private_eval := true;
        IF NEW.exclusion_reason IS NULL OR btrim(NEW.exclusion_reason) = '' THEN
            NEW.exclusion_reason := '입력/출력이 모델 학습에 사용됨 — 비공개 코드·운영/고객 데이터 평가 기본 제외';
        END IF;
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_llm_model_candidates_enforce_exclusion
    BEFORE INSERT OR UPDATE ON llm_model_candidates
    FOR EACH ROW EXECUTE FUNCTION llm_model_candidates_enforce_exclusion();

CREATE TABLE IF NOT EXISTS llm_model_comparisons (
    id                          BIGSERIAL PRIMARY KEY,
    milestone_id                UUID,
    surface                     TEXT NOT NULL,
    task_suite_key              TEXT NOT NULL,
    incumbent_provider          TEXT NOT NULL,
    incumbent_model_id          TEXT NOT NULL,
    incumbent_version_pin       TEXT,
    candidate_id                BIGINT NOT NULL REFERENCES llm_model_candidates(id) ON DELETE RESTRICT,
    candidate_version_pin       TEXT,
    min_sample_size             INTEGER NOT NULL,
    sample_size                 INTEGER NOT NULL DEFAULT 0,
    noninferiority_margin       JSONB,
    metrics_incumbent           JSONB,
    metrics_candidate           JSONB,
    verdict                     TEXT,
    verdict_reason              TEXT,
    cost_per_success_incumbent  NUMERIC(14,6),
    cost_per_success_candidate  NUMERIC(14,6),
    savings_pct                 NUMERIC(7,3),
    cost_source                 TEXT,
    evidence_refs               TEXT[] NOT NULL DEFAULT '{}',
    ran_at                      TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_llm_model_comparisons_surface
        CHECK (surface IN ('chat','runner','terminal_cli','service')),
    CONSTRAINT ck_llm_model_comparisons_verdict
        CHECK (verdict IS NULL OR verdict IN
            ('equivalent','candidate_better','candidate_worse','insufficient_sample','not_run')),
    CONSTRAINT ck_llm_model_comparisons_sample
        CHECK (min_sample_size > 0 AND sample_size >= 0),
    -- 비열등 한계는 시험 전에 선언한다. 비어 있으면 판정을 쓸 수 없다.
    CONSTRAINT ck_llm_model_comparisons_margin_first
        CHECK (verdict IS NULL OR (
            noninferiority_margin IS NOT NULL
            AND jsonb_typeof(noninferiority_margin) = 'object'
            AND noninferiority_margin <> '{}'::jsonb)),
    -- 표본 부족은 미검증이다. 결론(동등/우세/열위)을 저장할 수 없다.
    CONSTRAINT ck_llm_model_comparisons_min_sample
        CHECK (verdict IS NULL OR sample_size >= min_sample_size
            OR verdict IN ('insufficient_sample','not_run'))
);

CREATE INDEX IF NOT EXISTS idx_llm_model_comparisons_surface_ran
    ON llm_model_comparisons(surface, ran_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_model_comparisons_candidate
    ON llm_model_comparisons(candidate_id);

-- 시드: https://dev.meta.ai/docs/pricing-rate-limits 직접 조회 2026-09-30 02:0x KST (분 단위는 02:00 으로 기록).
-- Muse Code 구독 금액은 공식 문서에 없다 → NULL + unverified_official. 추정 금액을 넣지 않는다.
INSERT INTO llm_model_candidates (
    provider, model_id, product_name, surface_scope, official_url, verified_at, model_version,
    region_scope, quota_rpm, quota_tpm,
    price_input_per_1m, price_cached_input_per_1m, price_output_per_1m, price_currency,
    subscription_price_per_month, price_status, pricing_kind,
    training_use, training_terms_url, retention_note, extra_charges,
    excluded_from_private_eval, exclusion_reason, status, notes
) VALUES (
    'meta', 'muse-spark-1.3', 'Muse Spark 1.3 (Standard tier)',
    ARRAY['chat','runner','service'], 'https://dev.meta.ai/docs/pricing-rate-limits',
    '2026-09-30 02:00:00+09', '1.3',
    NULL, 3000, 4000000,
    1.25, 0.15, 4.25, 'USD',
    NULL, 'official_verified', 'api_per_token',
    'not_used', 'https://dev.meta.ai/docs/pricing-rate-limits',
    'your prompts and completions are not used to train Meta models',
    '{"web_search_per_1000_queries_usd": 2.50, "web_search_billing": "additional_to_tokens", "long_context_premium": false, "steering_context_billed": false}'::jsonb,
    false, NULL, 'candidate',
    '쿼터는 팀 단위(API 키 단위 아님). 동일 단가 적용 버전: muse-spark-1.2, muse-spark-1.1.'
) ON CONFLICT (provider, model_id) DO NOTHING;

INSERT INTO llm_model_candidates (
    provider, model_id, product_name, surface_scope, official_url, verified_at, model_version,
    region_scope, quota_rpm, quota_tpm,
    price_input_per_1m, price_cached_input_per_1m, price_output_per_1m, price_currency,
    subscription_price_per_month, price_status, pricing_kind,
    training_use, training_terms_url, retention_note, extra_charges,
    excluded_from_private_eval, exclusion_reason, status, notes
) VALUES (
    'meta', 'muse-spark-1.3-contributor', 'Muse Spark 1.3 (Contributor tier)',
    ARRAY['chat','runner','service'], 'https://dev.meta.ai/docs/pricing-rate-limits',
    '2026-09-30 02:00:00+09', '1.3',
    NULL, 100, 3000000,
    0.10, 0.002, 0.20, 'USD',
    NULL, 'official_verified', 'api_per_token',
    'used_for_training', 'https://dev.meta.ai/docs/pricing-rate-limits',
    '프롬프트·완성물이 향후 Meta 모델 학습에 사용됨',
    '{"web_search_per_1000_queries_usd": 2.50, "web_search_billing": "additional_to_tokens", "long_context_premium": false, "steering_context_billed": false}'::jsonb,
    true, '입력/출력이 Meta 모델 학습에 사용됨 — 비공개 코드·운영/고객 데이터 평가 기본 제외', 'candidate',
    '동일 단가 적용 버전: muse-spark-1.2-contributor.'
) ON CONFLICT (provider, model_id) DO NOTHING;

INSERT INTO llm_model_candidates (
    provider, model_id, product_name, surface_scope, official_url, verified_at, model_version,
    region_scope, quota_rpm, quota_tpm,
    price_input_per_1m, price_cached_input_per_1m, price_output_per_1m, price_currency,
    subscription_price_per_month, price_status, pricing_kind,
    training_use, training_terms_url, retention_note, extra_charges,
    excluded_from_private_eval, exclusion_reason, status, notes
) VALUES (
    'meta', 'muse-code', 'Muse Code (subscription)',
    ARRAY['terminal_cli'], 'https://dev.meta.ai/docs/muse-code/subscriptions',
    '2026-09-30 02:00:00+09', NULL,
    '지역에 따라 혜택·제공 여부가 다르며 온보딩 중에 표시됨', NULL, NULL,
    NULL, NULL, NULL, 'USD',
    NULL, 'unverified_official', 'subscription',
    'unknown', NULL, NULL,
    '{"plans": ["Everyday Usage", "High Usage", "Power Usage"], "relative_usage": {"Everyday Usage": 1, "High Usage": 5, "Power Usage": 20}, "everyday_prompts_per_5h": "10-50", "subscription_applies_to": "Muse Code CLI credentials only", "extra_api_keys_billing": "pay_as_you_go"}'::jsonb,
    false, NULL, 'candidate',
    '구독 금액은 공식 문서에 표기되지 않음(추정 금액 미기록). 구독료와 추가 API 키 요금은 서로 다른 계정 경로.'
) ON CONFLICT (provider, model_id) DO NOTHING;

COMMIT;
