-- 자동화 초안(automation_drafts) + 이벤트 로그. 설계서 5절. Additive only, 멱등. DROP 없음.
-- 롤백은 테이블 삭제가 아니라 기능 플래그 AUTOMATION_DRAFTS_ENABLED=false 로 입구만 닫는다.
BEGIN;

CREATE TABLE IF NOT EXISTS automation_drafts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL,
  owner_user      text NOT NULL,              -- 요청자 (채팅 세션이 아니라 사람)
  session_id      uuid,                       -- 시작한 채팅 세션 (없을 수 있음: 홈 버튼)
  title           text NOT NULL,              -- 화면 이름 "쿠팡이츠 어제 매출 받기"
  intent_text     text NOT NULL,              -- 사용자가 쓴 원문
  intent          jsonb NOT NULL DEFAULT '{}',-- {site_name, url, domain, task, output, schedule}
  aliases         text[] NOT NULL DEFAULT '{}', -- 반복 사용 매칭 문구 ("어제 매출", "배달 매출")
  state           text NOT NULL,
  credential_id   uuid,                       -- agent_vault_credentials.id 참조만. 비밀값 금지
  credential_request_id uuid,                 -- 안전 입력 요청 id
  lane            text NOT NULL DEFAULT 'server', -- server | pc
  max_risk        text,                       -- schema.RISK_LEVELS
  demo_attempts   int  NOT NULL DEFAULT 0,
  recording_ref   jsonb NOT NULL DEFAULT '{}',-- 수집 단계(비밀값은 {{secret.*}} 참조로만)
  evidence        jsonb NOT NULL DEFAULT '{}',-- {final_screenshot_url, preview_rows(≤5), screen_verified}
  registration_ids uuid[] NOT NULL DEFAULT '{}',
  recipe_names    text[] NOT NULL DEFAULT '{}',
  schedule_job_id text,
  failure_code    text,                       -- auth_failed|challenge|blocked|layout_changed|empty_result|transient
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS automation_drafts_tenant_state ON automation_drafts (tenant_id, state, updated_at DESC);

CREATE TABLE IF NOT EXISTS automation_draft_events (
  id          bigserial PRIMARY KEY,
  draft_id    uuid NOT NULL REFERENCES automation_drafts(id) ON DELETE CASCADE,
  event_type  text NOT NULL,   -- PRD 8절 목록
  from_state  text,
  to_state    text,
  actor       text,
  detail      jsonb NOT NULL DEFAULT '{}', -- 비밀값 금지 (저장 전 마스킹 검사)
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS automation_draft_events_draft ON automation_draft_events (draft_id, id);

COMMIT;
