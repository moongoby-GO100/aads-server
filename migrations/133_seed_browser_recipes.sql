-- AADS-APILESS-AUTH-AUTOMATION-P0: baseline BrowserRecipe seeds.
-- Additive/idempotent seed for OHVIS login smoke and Ddangyo approved CAPTCHA collection.

WITH target_tenant AS (
    SELECT id AS tenant_id
      FROM tenants
     WHERE name = 'AADS Internal'
     ORDER BY created_at
     LIMIT 1
)
INSERT INTO browser_recipes (
    tenant_id, recipe_id, version, title, service, allowed_origins, work_key_template,
    runtime_policy, concurrency_policy, resource_policy, login_steps, challenge_policy,
    navigation_steps, capture_rules, parser_id, upload_rules, risk_actions, verifier,
    fallbacks, enabled, version_hash, created_by, updated_at
)
SELECT
    tenant_id,
    'ohvis.login.basic',
    'v1',
    'OHVIS login smoke recipe',
    'ohvis',
    '["https://aads.newtalk.kr"]'::jsonb,
    'ohvis-login',
    '{"runtime":"self_hosted_playwright"}'::jsonb,
    '{"max_parallel_runs":2,"queue_strategy":"fifo","conflict_keys":["service","origin","work_key"]}'::jsonb,
    '{"runtime":"self_hosted_playwright","max_browser_contexts":2,"max_memory_mb":1536,"max_runtime_seconds":600,"artifact_budget_mb":512}'::jsonb,
    '[{"action":"navigate","url":"https://aads.newtalk.kr/login"},{"action":"vault_autofill","fields":["email","password"]},{"action":"click","selector":"button[type=submit]"}]'::jsonb,
    '{"otp":"approval_token","captcha":"approval_scoped_model_input"}'::jsonb,
    '[{"action":"wait_for_url","url_contains":"/chat"}]'::jsonb,
    '{"screenshot":true,"dom_snapshot":true}'::jsonb,
    'ohvis.login_status.v1',
    '{}'::jsonb,
    '[{"action_type":"login_submit","summary":"submit OHVIS login form"},{"action_type":"captcha_model_analysis","summary":"read approved challenge if OHVIS login presents CAPTCHA"}]'::jsonb,
    '{"success_url_contains":["/chat","/admin","/"],"must_not_contain":["login failed"]}'::jsonb,
    '{"browser_bridge":"pc_agent","manual_takeover":"approval_required"}'::jsonb,
    TRUE,
    '8ebd9feda07bef7face3fc9790cdaad22f9271a34930b1939e5ed580e585c0be',
    'ceo',
    NOW()
FROM target_tenant
ON CONFLICT (tenant_id, recipe_id, version) DO UPDATE
   SET title = EXCLUDED.title,
       service = EXCLUDED.service,
       allowed_origins = EXCLUDED.allowed_origins,
       work_key_template = EXCLUDED.work_key_template,
       runtime_policy = EXCLUDED.runtime_policy,
       concurrency_policy = EXCLUDED.concurrency_policy,
       resource_policy = EXCLUDED.resource_policy,
       login_steps = EXCLUDED.login_steps,
       challenge_policy = EXCLUDED.challenge_policy,
       navigation_steps = EXCLUDED.navigation_steps,
       capture_rules = EXCLUDED.capture_rules,
       parser_id = EXCLUDED.parser_id,
       upload_rules = EXCLUDED.upload_rules,
       risk_actions = EXCLUDED.risk_actions,
       verifier = EXCLUDED.verifier,
       fallbacks = EXCLUDED.fallbacks,
       enabled = EXCLUDED.enabled,
       version_hash = EXCLUDED.version_hash,
       updated_at = NOW();

WITH target_tenant AS (
    SELECT id AS tenant_id
      FROM tenants
     WHERE name = 'AADS Internal'
     ORDER BY created_at
     LIMIT 1
)
INSERT INTO browser_recipes (
    tenant_id, recipe_id, version, title, service, allowed_origins, work_key_template,
    runtime_policy, concurrency_policy, resource_policy, login_steps, challenge_policy,
    navigation_steps, capture_rules, parser_id, upload_rules, risk_actions, verifier,
    fallbacks, enabled, version_hash, created_by, updated_at
)
SELECT
    tenant_id,
    'delivery.ddangyo.sales_collect',
    'v1',
    'Ddangyo sales collection with approval-scoped CAPTCHA',
    'delivery',
    '["https://boss.ddangyo.com"]'::jsonb,
    'yeoljeong-delivery-ddangyo',
    '{"runtime":"pc_agent"}'::jsonb,
    '{"max_parallel_runs":1,"queue_strategy":"latest_only","conflict_keys":["service","origin","work_key"]}'::jsonb,
    '{"runtime":"pc_agent","max_browser_contexts":1,"max_memory_mb":1024,"max_runtime_seconds":1800,"artifact_budget_mb":1024}'::jsonb,
    '[{"action":"navigate","url":"https://boss.ddangyo.com"},{"action":"vault_autofill","fields":["username","password"]},{"action":"click","selector":"button[type=submit]"}]'::jsonb,
    '{"captcha":"approval_scoped_model_input","approval_scope_required":["origin","work_key","page_url","challenge_kind"]}'::jsonb,
    '[{"action":"open_sales_menu"},{"action":"set_date_range"},{"action":"search"}]'::jsonb,
    '{"download":true,"dom_table":true,"screenshot_hash":true}'::jsonb,
    'delivery.ddangyo.sales_collect.v1',
    '{"file_hash_required":true}'::jsonb,
    '[{"action_type":"captcha_model_analysis","summary":"read approved ddangyo numeric CAPTCHA"},{"action_type":"file_upload","summary":"upload approved settlement attachment"}]'::jsonb,
    '{"min_rows":1,"required_fields":["order_date","amount","store_name"]}'::jsonb,
    '{"selector_repair":"llm_dom_analysis","human_takeover":"approval_required"}'::jsonb,
    TRUE,
    'fedee2c8f618a391d278ef31488ae0dcd131e06560efa6407a13e1ed5f6a624f',
    'ceo',
    NOW()
FROM target_tenant
ON CONFLICT (tenant_id, recipe_id, version) DO UPDATE
   SET title = EXCLUDED.title,
       service = EXCLUDED.service,
       allowed_origins = EXCLUDED.allowed_origins,
       work_key_template = EXCLUDED.work_key_template,
       runtime_policy = EXCLUDED.runtime_policy,
       concurrency_policy = EXCLUDED.concurrency_policy,
       resource_policy = EXCLUDED.resource_policy,
       login_steps = EXCLUDED.login_steps,
       challenge_policy = EXCLUDED.challenge_policy,
       navigation_steps = EXCLUDED.navigation_steps,
       capture_rules = EXCLUDED.capture_rules,
       parser_id = EXCLUDED.parser_id,
       upload_rules = EXCLUDED.upload_rules,
       risk_actions = EXCLUDED.risk_actions,
       verifier = EXCLUDED.verifier,
       fallbacks = EXCLUDED.fallbacks,
       enabled = EXCLUDED.enabled,
       version_hash = EXCLUDED.version_hash,
       updated_at = NOW();
