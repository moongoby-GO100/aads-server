-- CLI 모델(codex CLI) 신규 모델 자동등록 (AADS-CLI-MODEL-AUTOREG-20260930).
-- llm_model_candidates 에 CLI 발견·실호출 검증 상태를 싣는다. 추가 전용, 여러 번 실행해도 결과가 같다.
--   discovered      : codex models_cache 에서 발견, 아직 실호출 검증 전
--   verified        : codex exec 실호출 성공 (llm_models codex 행 is_executable=true)
--   blocked_account : "not supported when using Codex with a ChatGPT account" — 24시간 후 재시도
--   probe_failed    : 그 밖의 실패 — 24시간 후 재시도
-- 목록에 있다고 실행 가능한 것이 아니다(2026-09-30 gpt-6.1-sol 실측) — 발견과 실행가능 판정을 분리한다.
-- status CHECK 는 값 목록만 넓힌다(기존 행·값 불변). 제약 교체 외 데이터 삭제 없음.
BEGIN;

ALTER TABLE llm_model_candidates ADD COLUMN IF NOT EXISTS discovery_source TEXT;
ALTER TABLE llm_model_candidates ADD COLUMN IF NOT EXISTS last_probe_at TIMESTAMPTZ;

ALTER TABLE llm_model_candidates DROP CONSTRAINT IF EXISTS ck_llm_model_candidates_status;
ALTER TABLE llm_model_candidates ADD CONSTRAINT ck_llm_model_candidates_status
    CHECK (status IN ('candidate','testing','approved','rejected','retired',
                      'discovered','verified','blocked_account','probe_failed'));

CREATE INDEX IF NOT EXISTS idx_llm_model_candidates_probe
    ON llm_model_candidates(provider, status, last_probe_at);

COMMIT;
