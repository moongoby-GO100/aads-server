-- 사이트 차단과 credential별 일일 제출 횟수만 저장. 비밀값 없음.
CREATE TABLE IF NOT EXISTS browser_login_policy (
 tenant_id uuid NOT NULL,
 policy_key text NOT NULL,
 day date NOT NULL,
 attempts integer NOT NULL DEFAULT 0,
 reason text NOT NULL DEFAULT '',
 PRIMARY KEY (tenant_id, policy_key, day)
);

-- Old blocks without an expiry are intentionally released on upgrade.
ALTER TABLE browser_login_policy ADD COLUMN IF NOT EXISTS blocked_until timestamptz;
