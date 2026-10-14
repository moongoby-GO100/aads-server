-- Rollback of 20261014_obys_auth_password_recovery.sql (obys_auth DB only).
-- 순서: 먼저 앱을 이전 릴리스로 되돌린 뒤(scripts/deploy_acct_app_cafe24.sh rollback ...) 이 파일을 적용한다.
-- 새 코드가 떠 있는 채로 적용하면 로그인 경로의 세션 상태 조회가 컬럼 부재를 감지해 가드를 끄므로 장애는 없지만,
-- 발급된 임시 비밀번호와 재설정 요청·감사 기록은 사라진다(비밀번호 해시·이메일 원문은 원래 그 테이블에 없다).
BEGIN;
DROP TABLE IF EXISTS public.obys_password_reset_audit;
DROP TABLE IF EXISTS public.obys_password_reset_requests;
ALTER TABLE public.saas_users DROP COLUMN IF EXISTS password_changed_at;
ALTER TABLE public.saas_users DROP COLUMN IF EXISTS session_version;
ALTER TABLE public.saas_users DROP COLUMN IF EXISTS must_change_password;
COMMIT;
