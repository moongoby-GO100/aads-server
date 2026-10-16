-- 20261016_external_ai_channels_turns 되돌림.
--
-- 두 테이블만 지운다(수집 스냅샷 사본이 사라진다). system_memory 의 conversation:* 적재는
-- 원본 경로라 그대로 남으므로 기존 소비자에는 영향이 없다.
-- 순서: 자식(turns) 먼저, 부모(channels) 나중.
BEGIN;

DROP TABLE IF EXISTS external_ai_turns;
DROP TABLE IF EXISTS external_ai_channels;

COMMIT;
