-- codex 계정 서버측 토큰 폐기(401 token_revoked) 사유 보존.
--
-- 2026-10-01 실측: CODEX_OAUTH_JINAH 는 로컬 토큰이 살아 있는데 서버가 401 로 거부했고
-- 스냅샷은 auth_usable=true 였다. auth_usable=false 와 함께 "왜" 와 "언제" 를 남긴다.
-- 사유는 고정 태그(예: '401 token_revoked')만 저장한다 — 서버 응답 원문·토큰은 넣지 않는다.
--
-- scripts/codex_usage.py 는 이 컬럼이 없는 DB 에서도 동작한다(옛 UPSERT 로 후퇴).
-- 기존 컬럼은 건드리지 않는다.

ALTER TABLE codex_usage_snapshots ADD COLUMN IF NOT EXISTS auth_revoked_reason TEXT;
ALTER TABLE codex_usage_snapshots ADD COLUMN IF NOT EXISTS auth_revoked_at TIMESTAMPTZ;

COMMENT ON COLUMN codex_usage_snapshots.auth_revoked_reason IS
    '서버측 토큰 폐기 사유 태그(401 token_revoked 등). NULL 이면 폐기 아님.';
COMMENT ON COLUMN codex_usage_snapshots.auth_revoked_at IS
    '폐기를 처음 감지한 시각. 이후 auth.json 이 다시 쓰이면(재로그인) 낡은 판정으로 본다.';
