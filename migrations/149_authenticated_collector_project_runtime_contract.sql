-- AADS-AUTHENTICATED-COLLECTOR-PROJECT-RUNTIME-CONTRACT
-- Allow FOOD SaaS collector scopes and the local Windows Collector runtime.
-- No data deletion. Constraints are replaced idempotently so existing rows stay intact.

ALTER TABLE authenticated_site_profiles
    DROP CONSTRAINT IF EXISTS authenticated_site_profiles_project_key_check,
    ADD CONSTRAINT authenticated_site_profiles_project_key_check
        CHECK (project_key IN (
            'AADS','KIS','GO100','SF','NTV2','NAS',
            'STORE_ASSISTANT','MARKETING','BANKING','CUSTOM'
        ));

ALTER TABLE authenticated_site_profiles
    DROP CONSTRAINT IF EXISTS authenticated_site_profiles_runtime_check,
    ADD CONSTRAINT authenticated_site_profiles_runtime_check
        CHECK (runtime IN (
            'webview2','windows_collector','chrome_extension','chrome_cdp',
            'playwright_server','file_upload','official_api','manual_export'
        ));

ALTER TABLE browser_recipes
    DROP CONSTRAINT IF EXISTS browser_recipes_project_key_check,
    ADD CONSTRAINT browser_recipes_project_key_check
        CHECK (project_key IN (
            'AADS','KIS','GO100','SF','NTV2','NAS',
            'STORE_ASSISTANT','MARKETING','BANKING','CUSTOM'
        ));

ALTER TABLE browser_recipes
    DROP CONSTRAINT IF EXISTS browser_recipes_site_environment_check,
    ADD CONSTRAINT browser_recipes_site_environment_check
        CHECK (site_environment IN (
            'webview2','windows_collector','chrome_extension','chrome_cdp',
            'playwright_server','file_upload','official_api','manual_export'
        ));
