-- M7 canonical rollback is schema-only.  It cannot restore redacted source
-- identifiers, and must never be run against an operational database.
BEGIN;
DROP INDEX IF EXISTS idx_ops_skill_versions_site_scope;
DROP INDEX IF EXISTS idx_browser_live_facts_site_knowledge_scope;
DROP INDEX IF EXISTS idx_browser_recipes_site_knowledge_scope;
DROP INDEX IF EXISTS idx_memory_facts_site_knowledge_scope;
ALTER TABLE browser_live_facts
    DROP CONSTRAINT IF EXISTS browser_live_facts_source_url_hash_check,
    DROP CONSTRAINT IF EXISTS browser_live_facts_variant_key_hash_check;
COMMIT;
