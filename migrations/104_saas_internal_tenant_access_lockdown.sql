-- 104: Lock down AADS internal tenant access for public SaaS accounts.
-- Public signups must use customer tenants; the internal tenant is CEO/admin only.

ALTER TABLE saas_users
    ALTER COLUMN default_tenant_id DROP DEFAULT;

ALTER TABLE saas_users
    ALTER COLUMN default_tenant_id DROP NOT NULL;

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
     WHERE t.kind = 'customer'
       AND t.status = 'active'
       AND t.deleted_at IS NULL
       AND tm.status = 'active'
       AND tm.deleted_at IS NULL
     ORDER BY tm.user_id, tm.created_at ASC
)
UPDATE saas_users u
   SET default_tenant_id = (
           SELECT preferred_customer_tenant.tenant_id
             FROM preferred_customer_tenant
            WHERE preferred_customer_tenant.user_id = u.id
            LIMIT 1
       ),
       updated_at = now()
  FROM internal_tenant
 WHERE u.default_tenant_id = internal_tenant.id
   AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'owner');

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
       internal_tenant
 WHERE tm.tenant_id = internal_tenant.id
   AND tm.user_id = u.id
   AND tm.status = 'active'
   AND tm.deleted_at IS NULL
   AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'owner')
   AND tm.role NOT IN ('owner', 'admin');
