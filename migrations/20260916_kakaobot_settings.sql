-- AADS-AAG-DEBT-002: 카카오봇 발송 설정 (사용자 1행)
--
-- 화면(aads-dashboard/src/app/kakaobot/settings/page.tsx)은 저장 버튼을 눌러도
-- PUT 대상이 없어 매번 "저장 실패" 를 띄웠고, 다시 열면 DEFAULT_SETTINGS 로
-- 돌아갔다. 기본값은 그 화면의 DEFAULT_SETTINGS 와 같은 값이어야 한다.
--
-- app/api/kakao_bot.py 의 _KAKAO_SETTINGS_DDL 이 부팅 시 같은 DDL 을 한 번 더
-- 돌린다(IF NOT EXISTS). 정본은 이 파일이다.

CREATE TABLE IF NOT EXISTS kakaobot_settings (
    user_id VARCHAR(100) PRIMARY KEY,
    auto_send_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    default_tone VARCHAR(20) NOT NULL DEFAULT 'friendly',
    send_channel VARCHAR(20) NOT NULL DEFAULT 'kakao',
    send_time VARCHAR(5) NOT NULL DEFAULT '09:00',
    birthday_days_before INTEGER NOT NULL DEFAULT 0,
    anniversary_days_before INTEGER NOT NULL DEFAULT 0,
    greeting_frequency VARCHAR(20) NOT NULL DEFAULT 'monthly',
    marketing_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 발송 이력 조회(GET /api/v1/kakao-bot/history, /history/stats)는
-- kakaobot_scheduled 를 user_id + status 로 훑는다. 이력이 쌓이면 seq scan 이 된다.
CREATE INDEX IF NOT EXISTS idx_kakaobot_scheduled_user_status_sent
    ON kakaobot_scheduled (user_id, status, sent_at DESC);
