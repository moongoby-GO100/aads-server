-- session_relay 에 'queued' 상태를 추가한다.
--
-- 왜 (2026-09-17, 대표님 "2번은 즉시 반영해")
--   ask_session 은 대상이 작업 중이면 target_busy 로 **반려**하고 끝냈다.
--   실측: session_relay 123건 중 회신 8건(6.5%) — pending 52 · failed 63.
--   세션 d19a0e9e 는 목표관리자에게 두 번 보내 두 번 다 반려되고
--   "마일스톤 문제는 아직 전달되지 않았습니다" 로 끝났다.
--
--   반려가 오탐은 아니었다. 그 순간 running 9건 전부 리스가 살아 있는 진짜
--   작업 중이었다. 문제는 **오래 일하는 세션에는 영영 못 닿는다**는 것이다
--   — 목표관리자 36분, #119 전략관리자 52분째였다.
--
--   끼어들지 않는다는 판단은 그대로 지킨다(진행 중 응답이 깨진다).
--   지금 보내지 않을 뿐, 끝나면 보낸다.
--
-- 상태 흐름
--   queued  → (대상이 한가해짐) → pending → answered
--                                        └→ failed
--   queued  → (6시간 초과)       → failed(queue_expired)

ALTER TABLE session_relay DROP CONSTRAINT IF EXISTS session_relay_status_chk;

ALTER TABLE session_relay
  ADD CONSTRAINT session_relay_status_chk
  CHECK (status = ANY (ARRAY['queued'::text, 'pending'::text, 'answered'::text,
                             'failed'::text, 'blocked'::text]));

-- 배달기는 대상별로 가장 오래된 한 건만 집는다. 그 조회를 받쳐 준다.
CREATE INDEX IF NOT EXISTS idx_session_relay_queued
    ON session_relay (target_session_id, created_at)
    WHERE status = 'queued';
