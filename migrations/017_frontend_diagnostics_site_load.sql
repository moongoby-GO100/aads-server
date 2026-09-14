-- 측정 맥락을 같이 남긴다.
--
-- 2026-09-14 배포 세 번 직후에 잰 값에서 LCP 가 +124% 로 찍혔다. 같은 순간
-- 아무 일도 하지 않는 /api/v1/health 가 2,011ms 였으니 프론트가 나빠진 게
-- 아니라 서버가 밀리고 있었다. 부하를 기록하지 않으면 이 둘을 구분할 수 없고,
-- 오독한 회귀는 멀쩡한 배포를 막는다.
--
-- site 는 대상 분리용이다. 한 DB 에 AADS·GO100 결과가 같이 쌓이므로 라우트만으로
-- 구분하면 "/" 가 서로 섞인다.
ALTER TABLE frontend_diagnostic_runs  ADD COLUMN IF NOT EXISTS site varchar(32) NOT NULL DEFAULT 'aads';
ALTER TABLE frontend_diagnostic_runs  ADD COLUMN IF NOT EXISTS load_info jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE frontend_diagnostic_runs  ADD COLUMN IF NOT EXISTS measurement_noisy text NOT NULL DEFAULT '';
ALTER TABLE frontend_diagnostic_pages ADD COLUMN IF NOT EXISTS site varchar(32) NOT NULL DEFAULT 'aads';

DROP INDEX IF EXISTS idx_fd_pages_route_time;
CREATE INDEX IF NOT EXISTS idx_fd_pages_site_route_time ON frontend_diagnostic_pages (site, route, created_at DESC);
