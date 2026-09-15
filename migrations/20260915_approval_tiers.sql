-- 승인 게이트 3단계 (2026-09-15 대표님 지시)
--
-- 대기 8건 중 진짜 승인 대상은 2건이었다. 나머지는 읽기 명령, 문서 작성,
-- 목표 정리였는데 전부 `[실매매] high` 로 올라왔다. 두 게이트가 한 함수를
-- 공유하면서 프리픽스와 위험도를 상수로 박아 넣은 탓이다.
--
-- 어디서 올라왔는지(gate_source)와 막을 것인지 알릴 것인지(tier)를 나눈다.
ALTER TABLE agent_permission_requests
    ADD COLUMN IF NOT EXISTS gate_source text NOT NULL DEFAULT 'live_trading',
    ADD COLUMN IF NOT EXISTS tier        text NOT NULL DEFAULT 'approve';

-- 지나간 판정은 소급해 고치지 않는다. 그때 그렇게 판정했다는 것이 기록이다.

CREATE INDEX IF NOT EXISTS idx_agent_permission_tier
    ON agent_permission_requests (tier, decision, created_at DESC);
