-- 107: Enforce the final internal tenant allowlist.
-- Only ceo/admin/system principals may keep active access to the internal tenant.

WITH internal_tenant AS (
    SELECT id
      FROM tenants
     WHERE slug = 'internal'
       AND deleted_at IS NULL
     LIMIT 1
),
preferred_customer_tenant AS (
    SELECT DISTINCT ON (tm.user_id)
           tm.user_id,
           tm.tenant_id
      FROM tenant_memberships tm
      JOIN tenants t ON t.id = tm.tenant_id
      JOIN saas_users u ON u.id = tm.user_id
     WHERE t.kind = 'customer'
       AND t.status = 'active'
       AND t.deleted_at IS NULL
       AND tm.status = 'active'
       AND tm.deleted_at IS NULL
       AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system')
     ORDER BY tm.user_id, tm.created_at ASC
)
UPDATE saas_users u
   SET default_tenant_id = pct.tenant_id,
       updated_at = now()
  FROM internal_tenant it,
       preferred_customer_tenant pct
 WHERE u.id = pct.user_id
   AND u.default_tenant_id = it.id
   AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system');

WITH internal_tenant AS (
    SELECT id
      FROM tenants
     WHERE slug = 'internal'
       AND deleted_at IS NULL
     LIMIT 1
)
UPDATE saas_users u
   SET default_tenant_id = NULL,
       updated_at = now()
  FROM internal_tenant it
 WHERE u.default_tenant_id = it.id
   AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system');

WITH internal_tenant AS (
    SELECT id
      FROM tenants
     WHERE slug = 'internal'
       AND deleted_at IS NULL
     LIMIT 1
)
UPDATE tenant_memberships tm
   SET status = 'removed',
       deleted_at = COALESCE(tm.deleted_at, now()),
       updated_at = now()
  FROM saas_users u,
       internal_tenant it
 WHERE tm.tenant_id = it.id
   AND tm.user_id = u.id
   AND tm.status = 'active'
   AND tm.deleted_at IS NULL
   AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system');
