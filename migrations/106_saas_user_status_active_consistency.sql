-- 106: Keep SaaS user status and active flags consistent.
-- Deleted/suspended users must not be counted as active SaaS users.

UPDATE saas_users
   SET is_active = FALSE,
       deleted_at = COALESCE(deleted_at, now()),
       updated_at = now()
 WHERE COALESCE(status, 'active') = 'deleted'
   AND is_active IS TRUE;

UPDATE saas_users
   SET is_active = FALSE,
       updated_at = now()
 WHERE COALESCE(status, 'active') = 'suspended'
   AND is_active IS TRUE;
