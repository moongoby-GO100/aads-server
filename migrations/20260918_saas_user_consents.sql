-- 약관·개인정보 동의 이력 (ACCT-LAYOUT-003 PRD §4, G-3) + last_login_at 갱신 선행 조건 (G-5)
--
-- 2026-09-18 실측: saas_users 88명 중 동의 이력을 저장하는 테이블이 없다.
-- last_login_at 은 PRD 실측 표에서 "0건 갱신" 으로 측정됐지만 이 저장소의
-- migrations/ 에는 그 컬럼을 만든 흔적이 없다 — 과거에 임시로 추가됐을 수
-- 있으므로 멱등하게 다시 선언해 둔다(있으면 무시, 없으면 생성).

CREATE TABLE IF NOT EXISTS saas_user_consents (
  id            BIGSERIAL PRIMARY KEY,
  user_id       TEXT NOT NULL,
  consent_key   TEXT NOT NULL,     -- terms / privacy / marketing / age14
  version       TEXT NOT NULL,     -- 약관 버전 (문구 변경 추적)
  agreed        BOOLEAN NOT NULL,
  agreed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ip            INET,
  user_agent    TEXT
);

CREATE INDEX IF NOT EXISTS idx_saas_user_consents_lookup
    ON saas_user_consents (user_id, consent_key, version);

ALTER TABLE saas_users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
