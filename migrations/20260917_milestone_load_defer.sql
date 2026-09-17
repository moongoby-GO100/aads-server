-- 부하 게이트로 마일스톤 지시가 밀리기 시작한 시각.
--
-- 2026-09-17. contabo14 는 장중에 매매 엔진 둘과 postgres·러너가 같이 돌아
-- 부하가 3.6~4.2배로 유지되는데 부하 게이트 기준은 2.0배다. GO100 은
-- 09:09 KST 이후 사이클마다 전건이 goal_dispatch_load_gated 로 밀렸고,
-- 장이 열려 있는 동안 오케스트레이션이 통째로 멎었다.
--
-- 미루기에 상한을 두려면 "언제부터 밀렸는가" 가 남아야 한다. 메모리에 두면
-- 배포 때마다 0 으로 돌아가 상한이 영원히 오지 않는다.
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS load_deferred_since timestamptz;
