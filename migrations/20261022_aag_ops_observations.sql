-- AAG S2: 운영 서버 관측(crontab -l / systemctl list-timers)을 저장소 그래프와 분리해 둔다.
-- Additive only. 저장소 그래프(aag_graph_snapshots_v2)는 건드리지 않는다.
-- 이 파일은 작성만 한다 — 적용은 릴리스 절차(롤아웃)에서 한다.

CREATE TABLE IF NOT EXISTS aag_ops_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    host TEXT NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL,
    schema_version TEXT NOT NULL,
    content_digest CHAR(64) NOT NULL,
    crontab_entries INTEGER NOT NULL DEFAULT 0,
    active_timers INTEGER NOT NULL DEFAULT 0,
    -- 수집 명령별 성공 여부. 실패한 명령은 "없음" 이 아니라 "관측하지 못함" 이다.
    command_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    observation JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 같은 관측 이벤트 재전송만 중복 제거한다. 상태 복귀는 별도 관측이다.
    CONSTRAINT aag_ops_observations_event_uniq UNIQUE (project, host, collected_at, content_digest)
);

CREATE INDEX IF NOT EXISTS aag_ops_observations_latest_idx
    ON aag_ops_observations (project, host, collected_at DESC);
