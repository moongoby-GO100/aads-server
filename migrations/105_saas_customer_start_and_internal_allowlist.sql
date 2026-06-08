-- 105: Force public SaaS users into customer tenants and restrict internal tenant.
-- Internal tenant remains available only to CEO/admin/system principals.

ALTER TABLE saas_users
    ALTER COLUMN default_tenant_id DROP DEFAULT;

ALTER TABLE saas_users
    ALTER COLUMN default_tenant_id DROP NOT NULL;

WITH eligible_users AS (
    SELECT u.id,
           u.email,
           COALESCE(NULLIF(u.name, ''), split_part(u.email, '@', 1), 'User') AS display_name
      FROM saas_users u
     WHERE COALESCE(u.status, 'active') = 'active'
       AND u.deleted_at IS NULL
       AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system')
       AND NOT EXISTS (
           SELECT 1
             FROM tenant_memberships tm
             JOIN tenants t ON t.id = tm.tenant_id
            WHERE tm.user_id = u.id
              AND tm.status = 'active'
              AND tm.deleted_at IS NULL
              AND t.kind = 'customer'
              AND t.status = 'active'
              AND t.deleted_at IS NULL
       )
)
INSERT INTO tenants (slug, name, kind, status, metadata, created_by)
SELECT lower(
           trim(
               both '-' FROM regexp_replace(split_part(eu.email, '@', 1), '[^a-zA-Z0-9-]+', '-', 'g')
           )
       ) || '-' || substr(md5(eu.id), 1, 8),
       eu.display_name || ' Workspace',
       'customer',
       'active',
       jsonb_build_object('plan_key', 'free', 'source', 'migration_105_customer_start'),
       eu.id
  FROM eligible_users eu
ON CONFLICT (slug) DO NOTHING;

WITH customer_tenant AS (
    SELECT DISTINCT ON (u.id)
           u.id AS user_id,
           t.id AS tenant_id
      FROM saas_users u
      JOIN tenants t ON t.created_by = u.id
     WHERE COALESCE(u.status, 'active') = 'active'
       AND u.deleted_at IS NULL
       AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system')
       AND t.kind = 'customer'
       AND t.status = 'active'
       AND t.deleted_at IS NULL
     ORDER BY u.id, t.created_at ASC
)
INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
SELECT ct.tenant_id,
       ct.user_id,
       'owner',
       'active'
  FROM customer_tenant ct
ON CONFLICT (tenant_id, user_id) DO UPDATE
   SET role = CASE
           WHEN tenant_memberships.role IN ('owner', 'admin') THEN tenant_memberships.role
           ELSE 'owner'
       END,
       status = 'active',
       deleted_at = NULL,
       updated_at = now();

WITH preferred_customer_tenant AS (
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
  FROM preferred_customer_tenant pct
 WHERE u.id = pct.user_id
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
       internal_tenant
 WHERE tm.tenant_id = internal_tenant.id
   AND tm.user_id = u.id
   AND tm.status = 'active'
   AND tm.deleted_at IS NULL
   AND COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system');

WITH internal_tenant AS (
    SELECT id
      FROM tenants
     WHERE slug = 'internal'
       AND deleted_at IS NULL
     LIMIT 1
)
UPDATE tenant_memberships tm
   SET role = CASE WHEN tm.role = 'owner' THEN 'owner' ELSE 'admin' END,
       status = 'active',
       deleted_at = NULL,
       updated_at = now()
  FROM saas_users u,
       internal_tenant
 WHERE tm.tenant_id = internal_tenant.id
   AND tm.user_id = u.id
   AND COALESCE(u.role, 'user') IN ('ceo', 'admin', 'system');
