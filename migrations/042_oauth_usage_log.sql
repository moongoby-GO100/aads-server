-- 042: OAuth 사용량 추적 테이블
-- AADS-192: OAuth(Claude Max) 사용량 분석 — 5시간/1주일 한도 추적
-- 2026-04-03

CREATE TABLE IF NOT EXISTS oauth_usage_log (
    id              BIGSERIAL PRIMARY KEY,
    account_slot    VARCHAR(20)   NOT NULL,      -- 'primary' / 'fallback'
    token_prefix    VARCHAR(20)   DEFAULT '',     -- 토큰 앞 12자 (식별용)
    model           VARCHAR(60)   NOT NULL,
    input_tokens    INT           NOT NULL DEFAULT 0,
    output_tokens   INT           NOT NULL DEFAULT 0,
    cache_creation_tokens INT     DEFAULT 0,
    cache_read_tokens     INT     DEFAULT 0,
    cost_usd        DECIMAL(10,6) DEFAULT 0,
    -- Rate-limit 헤더 (Anthropic API 응답에서 추출)
    rl_requests_limit       INT,
    rl_requests_remaining   INT,
    rl_requests_reset       TIMESTAMPTZ,
    rl_tokens_limit         INT,
    rl_tokens_remaining     INT,
    rl_tokens_reset         TIMESTAMPTZ,
    rl_input_tokens_limit       INT,
    rl_input_tokens_remaining   INT,
    rl_input_tokens_reset       TIMESTAMPTZ,
    rl_output_tokens_limit      INT,
    rl_output_tokens_remaining  INT,
    rl_output_tokens_reset      TIMESTAMPTZ,
    -- 호출 메타데이터
    call_source     VARCHAR(30)   DEFAULT '',     -- 'ceo_chat' / 'anthropic_client' / 'model_selector'
    session_id      VARCHAR(100)  DEFAULT '',
    error_code      VARCHAR(20),                  -- NULL=성공, '429', '401' 등
    duration_ms     INT           DEFAULT 0,      -- API 호출 소요시간
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- 계정별 시간순 조회 (5시간/1주일 윈도우)
CREATE INDEX IF NOT EXISTS idx_oauth_usage_account_time
    ON oauth_usage_log (account_slot, created_at DESC);

-- 모델별 집계
CREATE INDEX IF NOT EXISTS idx_oauth_usage_model_time
    ON oauth_usage_log (model, created_at DESC);

-- 7일 이전 자동 정리용 (선택적 cron)
CREATE INDEX IF NOT EXISTS idx_oauth_usage_created
    ON oauth_usage_log (created_at);
