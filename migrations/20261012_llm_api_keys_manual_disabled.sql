-- 구독계정 수동 켜기/끄기 표식 (AADS-LLM-ACCOUNT-ONOFF-API-20261012)
--
-- CEO 가 설정 화면에서 끈 계정은 rate_limit_recovery(60초 주기)가 되살리지 못하게
-- 표시한다. is_active=false 만으로는 "자동 복구 대상" 과 구분되지 않아서, 16:23 에 끈
-- id=2 가 34초 뒤 되살아났다.
--   manual_disabled     TRUE 이면 어떤 자동 경로도 is_active 를 TRUE 로 만들지 않는다.
--   manual_disabled_at  CEO 토글로 끈 시각
--   manual_disabled_by  끈 주체('ceo-settings')
--
-- 가산 전용·반복 적용 가능. 기존 행의 값은 건드리지 않는다(기본 FALSE) — 어느 계정을
-- 끌지는 CEO 가 화면에서 직접 정한다. 이전 코드는 새 컬럼을 읽지 않으므로 롤백에
-- 별도 DDL 이 필요 없다.
ALTER TABLE llm_api_keys ADD COLUMN IF NOT EXISTS manual_disabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE llm_api_keys ADD COLUMN IF NOT EXISTS manual_disabled_at TIMESTAMPTZ;
ALTER TABLE llm_api_keys ADD COLUMN IF NOT EXISTS manual_disabled_by TEXT;
