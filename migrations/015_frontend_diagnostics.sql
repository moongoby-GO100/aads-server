-- 프론트 진단기 결과 저장.
--
-- 측정값을 쌓기만 하면 아무도 보지 않는다. 저장하는 이유는 하나다 —
-- **직전 실행과 비교해 나빠진 것을 찾기 위해서**다. 그래서 회귀 비교에 필요한
-- 축(라우트, 릴리스, 시각)을 인덱스로 잡는다.

CREATE TABLE IF NOT EXISTS frontend_diagnostic_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    mode            varchar(16)  NOT NULL DEFAULT 'quick',
    release_sha     varchar(40)  NOT NULL DEFAULT '',
    base_url        text         NOT NULL DEFAULT '',
    pages_total     integer      NOT NULL DEFAULT 0,
    pages_ok        integer      NOT NULL DEFAULT 0,
    pages_failed    integer      NOT NULL DEFAULT 0,
    verdict         varchar(8)   NOT NULL DEFAULT 'pass',   -- pass | warn | fail
    summary         jsonb        NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_fd_runs_started ON frontend_diagnostic_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS frontend_diagnostic_pages (
    id              bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES frontend_diagnostic_runs(id) ON DELETE CASCADE,
    route           text NOT NULL,
    status          varchar(16) NOT NULL,   -- ok | auth_failed | error | timeout
    -- 페이지 로딩
    ttfb_ms         integer,
    dcl_ms          integer,
    fcp_ms          integer,
    lcp_ms          integer,
    load_ms         integer,
    transfer_bytes  bigint,
    -- 데이터 로딩
    api_calls       integer NOT NULL DEFAULT 0,
    api_total_ms    integer,
    api_slowest_ms  integer,
    api_slowest_url text,
    data_wait_ms    integer,
    serial_chain_ms integer,
    -- 결함
    console_errors  integer NOT NULL DEFAULT 0,
    page_errors     integer NOT NULL DEFAULT 0,
    failed_requests integer NOT NULL DEFAULT 0,
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- 회귀 비교는 "같은 라우트의 직전 값"을 찾는 질의다.
CREATE INDEX IF NOT EXISTS idx_fd_pages_route_time ON frontend_diagnostic_pages (route, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fd_pages_run ON frontend_diagnostic_pages (run_id);

CREATE TABLE IF NOT EXISTS frontend_diagnostic_findings (
    id              bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES frontend_diagnostic_runs(id) ON DELETE CASCADE,
    route           text NOT NULL,
    severity        varchar(8) NOT NULL,    -- critical | major | minor
    category        varchar(32) NOT NULL,
    title           text NOT NULL,
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fd_findings_run ON frontend_diagnostic_findings (run_id, severity);
