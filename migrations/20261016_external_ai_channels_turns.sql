-- AADS-EXTAI-M1-PROVIDER-GENERALIZE-20261016
-- 외부 AI 구독 채팅창 브릿지 M1 — 수집 대상 채널과 수집 스냅샷 테이블.
-- 되돌림: migrations/rollback/20261016_external_ai_channels_turns.down.sql
--
-- 순수 추가형이다. 기존 테이블을 ALTER/DROP 하지 않고, 멱등(IF NOT EXISTS)이다.
-- 기존 system_memory(conversation:*) 적재는 그대로 남고, 이 두 테이블은 그 옆에 이중 기록된다.
--
-- external_ai_channels : (provider, url) 하나가 채널 한 줄. last_status/last_reject_reason 은
--                        마지막 수집 시도의 결과(saved/duplicate/skipped/failed)와 거부 사유.
-- external_ai_turns    : 수집한 화면 스냅샷. (channel_id, content_hash) 가 같으면 같은 화면이므로
--                        다시 적재하지 않는다.
BEGIN;

CREATE TABLE IF NOT EXISTS external_ai_channels (
    id                 BIGSERIAL PRIMARY KEY,
    provider           TEXT NOT NULL
                       CHECK (provider IN ('genspark','chatgpt','gemini','claude','perplexity','other')),
    project            TEXT,
    channel_key        TEXT,
    url                TEXT NOT NULL,
    title              TEXT,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    last_collected_at  TIMESTAMPTZ,
    last_status        TEXT,
    last_reject_reason TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, url)
);

CREATE INDEX IF NOT EXISTS idx_external_ai_channels_project
    ON external_ai_channels (project);

CREATE TABLE IF NOT EXISTS external_ai_turns (
    id           BIGSERIAL PRIMARY KEY,
    channel_id   BIGINT NOT NULL REFERENCES external_ai_channels(id),
    provider     TEXT NOT NULL,
    project      TEXT,
    snapshot     TEXT NOT NULL,
    char_count   INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT,
    UNIQUE (channel_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_external_ai_turns_channel_collected
    ON external_ai_turns (channel_id, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_external_ai_turns_project_collected
    ON external_ai_turns (project, collected_at DESC);

COMMIT;
