-- 코덱스 계정별 사용량 스냅샷.
--
-- 왜 테이블이 필요한가 — 사용량 원본은 호스트의 rollout 파일인데, API 컨테이너에는
-- /root/.codex-accounts 도 /root/.codex-relay 도 마운트돼 있지 않다. 호스트에서 도는
-- scripts/codex_usage.py 가 긁어 여기에 적고, API/대시보드는 이 표만 본다.
--
-- 계정 1행만 유지한다(UPSERT). 이력이 필요해지면 그때 별도 표로 쌓는다 —
-- 10분마다 행을 늘리면 의미 없는 시계열이 쌓이기만 한다. 실제로 필요한 것은
-- "지금 얼마나 남았나" 하나다.
--
-- 설계: aads-docs/docs/PRD-LLM-ACCOUNT-RUNTIME-BINDING-v1.0.md

CREATE TABLE IF NOT EXISTS codex_usage_snapshots (
    key_name            VARCHAR(100) PRIMARY KEY
        REFERENCES llm_api_keys(key_name) ON DELETE CASCADE,
    used_percent        NUMERIC(5,1),
    window_minutes      INTEGER,
    -- 주간 한도가 풀리는 시각. rate_limits.primary.resets_at 원본이다.
    resets_at           TIMESTAMPTZ,
    -- 스냅샷을 찍은 시각. 지금과 벌어질수록 값을 믿을 수 없다 — 한도에 걸린
    -- 호출은 사용률을 갱신해주지 않아 값이 통째로 낡는다(실측 50~121시간).
    snapshot_at         TIMESTAMPTZ,
    ok_72h              INTEGER NOT NULL DEFAULT 0,
    limit_72h           INTEGER NOT NULL DEFAULT 0,
    sessions            INTEGER NOT NULL DEFAULT 0,
    tokens_recent       BIGINT  NOT NULL DEFAULT 0,
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE codex_usage_snapshots IS
    '코덱스 계정별 최신 사용량. scripts/codex_usage.py --sync 가 10분마다 갱신한다.';
