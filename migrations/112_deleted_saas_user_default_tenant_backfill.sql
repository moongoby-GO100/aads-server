-- 112: Backfill default tenants for deleted SaaS users.
-- Deleted users do not need active customer access, but keeping default_tenant_id
-- populated lets tenant-isolation audits close cleanly across all historical rows.

WITH missing_deleted_users AS (
    SELECT u.id,
           COALESCE(NULLIF(u.name, ''), 'Deleted User') AS display_name,
           u.deleted_at
      FROM saas_users u
     WHERE u.default_tenant_id IS NULL
       AND u.deleted_at IS NOT NULL
)
INSERT INTO tenants (slug, name, kind, status, metadata, created_by, deleted_at)
SELECT 'deleted-user-' || substr(md5(mdu.id), 1, 12),
       mdu.display_name || ' Archived Workspace',
       'customer',
       'archived',
       jsonb_build_object(
           'plan_key', 'free',
           'source', 'migration_112_deleted_user_default_tenant_backfill',
           'reason', 'deleted_user_tombstone'
       ),
       mdu.id,
       mdu.deleted_at
  FROM missing_deleted_users mdu
ON CONFLICT (slug) DO UPDATE
   SET metadata = tenants.metadata || EXCLUDED.metadata,
       updated_at = now();

WITH preferred_tenants AS (
    SELECT u.id AS user_id,
           t.id AS tenant_id,
           u.deleted_at
      FROM saas_users u
      JOIN tenants t
        ON t.slug = 'deleted-user-' || substr(md5(u.id), 1, 12)
       AND t.kind = 'customer'
     WHERE u.deleted_at IS NOT NULL
       AND (u.default_tenant_id IS NULL OR u.default_tenant_id = t.id)
)
INSERT INTO tenant_memberships (tenant_id, user_id, role, status, deleted_at)
SELECT pt.tenant_id,
       pt.user_id,
       'owner',
       'removed',
       COALESCE(pt.deleted_at, now())
  FROM preferred_tenants pt
ON CONFLICT (tenant_id, user_id) DO UPDATE
   SET role = 'owner',
       status = 'removed',
       deleted_at = COALESCE(tenant_memberships.deleted_at, EXCLUDED.deleted_at),
       updated_at = now();

WITH preferred_tenants AS (
    SELECT u.id AS user_id,
           t.id AS tenant_id
      FROM saas_users u
      JOIN tenants t
        ON t.slug = 'deleted-user-' || substr(md5(u.id), 1, 12)
       AND t.kind = 'customer'
     WHERE u.default_tenant_id IS NULL
       AND u.deleted_at IS NOT NULL
)
UPDATE saas_users u
   SET default_tenant_id = pt.tenant_id,
       updated_at = now()
  FROM preferred_tenants pt
 WHERE u.id = pt.user_id;
