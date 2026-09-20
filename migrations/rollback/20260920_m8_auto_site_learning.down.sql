-- Schema-only rollback. Canonical learned artifacts and skill versions are retained.
BEGIN;
DROP INDEX IF EXISTS idx_browser_site_learning_scope_lookup;
DROP TABLE IF EXISTS browser_site_learning_scopes;
DROP FUNCTION IF EXISTS enforce_browser_site_learning_scope_tenant();
COMMIT;
