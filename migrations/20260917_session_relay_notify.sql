-- 시스템 발신 알림의 중복 방지 키를 session_relay 에 붙인다.
--
-- 왜 (2026-09-17, CEO "텔레그램은 알림에서 제외한다. 담당 세션에 알림 주고
-- 조치할 수 있게 해라")
--   외부 시스템(GO100 cron, contabo14)이 담당 세션에 알림을 넣는 HTTP 경로를
--   신설했다(POST /api/v1/notifications/relay). cron 은 같은 알림을 5분마다
--   다시 보낸다 — 상태가 안 바뀌었으니 조건이 계속 참이다. 그대로 두면 담당
--   세션이 같은 알림으로 매 5분 깨어나 응답을 만든다(LLM 15회/task 예산이
--   알림만으로 소진된다).
--
--   그래서 dedup_key 로 창(기본 300초) 안의 재전송을 막는다. 키를 안 주면
--   역할·제목·본문·프로젝트 해시로 서버가 만든다(auto: 접두사).
--
-- 기존 ask_session 행에는 이 값이 NULL 이다 — 담당끼리 주고받는 질문은
-- 같은 질문이라도 두 번 물을 이유가 있으므로 중복 검사 대상이 아니다.

ALTER TABLE session_relay ADD COLUMN IF NOT EXISTS dedup_key text;

-- 중복 조회는 "시스템 발신 + 같은 키 + 최근 N초" 다. 그 조회만 받쳐 준다.
CREATE INDEX IF NOT EXISTS idx_session_relay_dedup
    ON session_relay (dedup_key, created_at DESC)
    WHERE dedup_key IS NOT NULL;
