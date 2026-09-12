-- 러너 작업이 어느 서버에서 실행됐는지 기록한다.
--
-- pipeline_jobs 에는 worker_model 과 runner_pid 만 있어 실행 서버를 알 수 없었다.
-- runner_pid 는 숫자라 어느 호스트의 PID 인지 구분되지 않는다. 2026-09-12 기준
-- 러너는 3대(contabo116=AADS, contabo14=GO100, cafe24_114=SF/NTV2/NAS)에서 돌고,
-- 담당 프로젝트가 겹치지 않아 project 로 역산은 가능하지만 그 매핑은 systemd
-- 환경변수에 있을 뿐 데이터가 아니다. 담당이 바뀌면 화면이 조용히 틀려진다.
--
-- 가산 마이그레이션이다. 기존 행은 NULL 로 남고, 조회 측은 NULL 일 때
-- project 매핑으로 폴백한다. 롤백은 컬럼 삭제만으로 끝난다.
ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS runner_host text;

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_runner_host_updated
    ON pipeline_jobs (runner_host, updated_at DESC)
    WHERE runner_host IS NOT NULL;

-- 러너 하트비트. 서버가 살아 있는지 원격 systemctl 조회 없이 DB 만으로 판단한다.
CREATE TABLE IF NOT EXISTS pipeline_runner_hosts (
    host            text PRIMARY KEY,
    projects        text NOT NULL DEFAULT '',
    engine_mode     text NOT NULL DEFAULT '',
    max_concurrent  int,
    last_seen_at    timestamptz NOT NULL DEFAULT NOW(),
    created_at      timestamptz NOT NULL DEFAULT NOW()
);
